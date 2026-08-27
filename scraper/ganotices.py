"""GA Foreclosure Notices scraper — georgiapublicnotice.com (Georgia Press Assoc).

Mirrors tnforeclosures.py architecture: the Georgia Press Association public
notice site is the same ASP.NET WebForms platform (identical GridView,
hdnPKValue rows, and a Turnstile + "I Agree, View Notice" detail gate).

Restricted to the N GA mountain counties at ~1700ft (option 2 of the build):
fannin, gilmer, lumpkin, rabun, towns, union, white. The search is scoped to
the "Tax Sales" popular category so only tax foreclosures (delinquent-property-
tax sales / tax-deed redemptions) are returned -- mortgage/bank foreclosures
are filtered out by the same tax-vs-mortgage classifier used for TN.
"""
from __future__ import annotations
import html as html_lib
import io
import re
import sys
import logging
from datetime import date, timedelta
from typing import Optional

from .base import BaseForeclosureScraper, PropertyData
from .config import (
    config,
    GAFORECLOSURES_BASE_URL,
    GAFORECLOSURES_TURNSTILE_SITE_KEY,
    GAFORECLOSURES_POPULAR_SEARCH_VALUE,
    GAFORECLOSURES_CATEGORIES,
    GA_MOUNTAIN_COUNTIES,
)

logger = logging.getLogger(__name__)

COUNTY_SET = set(GA_MOUNTAIN_COUNTIES)

# TN detail pages are gated by a Cloudflare Turnstile + a "View Notice" submit
# button (identical widget/key to ncnotices.com).
TURNSTILE_FIELD = 'input[name="cf-turnstile-response"]'
VIEW_NOTICE_BTN = 'input[name="ctl00$ContentPlaceHolder1$PublicNoticeDetailsBody1$btnViewNotice"]'
DOWNLOAD_LINK = 'a[id*="lnkDownload"]'
PER_PAGE_SELECT = 'select[name*="ddlPerPage"]'

# Street-address extraction from notice bodies (TNMap matches on address).
_ADDRESS_RE = re.compile(
    r"\b(\d{1,5}\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+){0,4}?\s*"
    r"(?:STREET|ST|AVENUE|AVE|BOULEVARD|BLVD|DRIVE|DR|ROAD|RD|LANE|LN|"
    r"HIGHWAY|HWY|COURT|CT|CIRCLE|CIR|PKWY|PIKE|WAY)\.?)",
    re.IGNORECASE,
)

# Parcel street address inside GA sheriff's-sale notices -- anchored on the
# "known as" / "located on" phrasing that precedes each parcel's street.
_GA_PARCEL_ADDR_RE = re.compile(
    r"(?:known as|located on)\s+"
    r"(\d{1,5}\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+){0,4}"
    r"\s+(?:STREET|ST|AVENUE|AVE|BLVD|DRIVE|DR|ROAD|RD|LANE|LN|HWY|"
    r"HIGHWAY|CT|CIRCLE|CIR|PKWY|PIKE|WAY|TRAIL|HEIGHTS|ESTATES|GASSE)\.?)",
    re.IGNORECASE,
)
# Fallback when only a parcel number is given (e.g. Towns County notices).
_GA_PARCEL_NO_RE = re.compile(r"Tax Map\s*&\s*Parcel[:\s]+([0-9A-Za-z]+)", re.IGNORECASE)

# Sale date inside a GA sheriff's-sale notice, e.g. "September 1st, 2026" or
# "the same being July 7, 2026". Used to de-duplicate the same county tax
# sale that the source posts under several grid rows.
_SALE_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE,
)

# GA tax sales are statutorily held on the FIRST TUESDAY of the month -- the
# notices phrase this as "first Tuesday in <Month> <Year>". We key off that so
# the sale date is correct even when the "the same being <date>" gloss is off.
_FIRST_TUESDAY_RE = re.compile(
    r"first\s+Tuesday\s+in\s+(January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+(\d{4})",
    re.IGNORECASE,
)

# Split a single (bundled) GA tax-sale notice into its per-parcel blocks.
# White County anchors each block with "File #: N Map/Parcel Number: <parcel>"
# and Towns County with "Map & Parcel: <parcel>". The parcel number (which may
# contain spaces, e.g. "062 189", or alphanumeric suffixes like "0002085A")
# is captured up to the following "Defendant" token that introduces the block.
_PARCEL_SPLIT_RE = re.compile(
    r"(?:File\s+#:\s*\d+\s*Map/Parcel Number:|Map\s*&\s*Parcel:|Map/Parcel Number:)\s*"
    r"(.+?)\s+Defendant",
    re.IGNORECASE,
)
# Acreage within a single parcel block, e.g. "containing 1.06 acres" or
# "being 6.00 acres, more or less".
_PARCEL_ACRES_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s+acres?\b", re.IGNORECASE)

# georgiapublicnotice.com (like tnpublicnotice.com) files mortgage/deed-of-
# trust (bank) foreclosures in the same "Tax Sales" / "Foreclosures" popular
# searches as genuine tax foreclosures. We must keep ONLY tax foreclosures
# (county tax-commissioner sales for delinquent property taxes) and drop
# mortgage/bank ones.
#
# Strong signals that a notice is a MORTGAGE / DEED-OF-TRUST (bank) sale.
_MORTGAGE_FC_PATTERNS = [
    r"deed\s+of\s+trust",
    r"substitute\s+trustee",
    r"owner\s+of\s+debt",
    r"\bbeneficiary\b",
    r"pursuant\s+to\s+(?:a\s+|the\s+)?deed\s+of\s+trust",
]
# Signals that a notice is a TAX foreclosure / tax sale (county trustee sale
# for delinquent property taxes, under T.C.A. ch. 67).
_TAX_FC_PATTERNS = [
    r"tax\s+sale",
    r"tax\s+foreclosure",
    r"tax\s+lien",
    r"delinquent\s+tax",
    r"for\s+delinquent\s+taxes",
    r"sale\s+of\s+real\s+property\s+for",
    r"tennessee\s+code\s+annotated\s*[§]?\s*67",
    r"\bt\.?c\.?a\.?\s*[§]?\s*67",
    r"section\s+67[-\s]",
    r"county\s+trustee",
]

# Signals that a notice is a QUIET-TITLE / TAX-REDEMPTION title-clearing
# proceeding -- a *post*-tax-sale action to clear title (e.g. "Petition To
# Establish Title Against All The World ... Tax Sale Redemption"), NOT an
# upcoming tax-sale foreclosure. These are filtered out; we only want active
# tax-sale foreclosures.
_QUIET_TITLE_PATTERNS = [
    r"petition\s+to\s+establish\s+title",
    r"establish\s+title\s+against\s+all\s+the\s+world",
    r"quiet\s+title",
    r"tax\s+sale\s+redemption",
    r"tax\s+deed\s+redemption",
    r"redeem(?:s|ed|ing)?\s+the\s+tax\s+deed",
    r"order\s+for\s+service\s+by\s+publication",
]

# Post-tax-sale proceedings that are NOT upcoming foreclosure sales. These
# reference an already-completed tax sale (excess/surplus-fund interpleaders,
# foreclosure of the equity of redemption, quiet-title) and must be dropped.
_POST_SALE_PATTERNS = [
    r"excess\s+funds",
    r"surplus\s+funds",
    r"interpleader",
    r"petition\s+for\s+interpleader",
    r"equity\s+of\s+redemption",
    r"foreclosure\s+of\s+equity",
]

# Civil / file / case action numbers (e.g. "CIVIL ACTION NO. SUCV2025000656",
# "FILE NO. SUCV2026000028"). Used to de-duplicate notices that are the same
# legal case but appear under different grid row ids.
_CASE_NO_RE = re.compile(
    r"(?:CIVIL ACTION|FILE|CASE|ACTION)\s+NO\.?\s*([A-Za-z]+\d+)",
    re.IGNORECASE,
)


class GanoticesScraper(BaseForeclosureScraper):
    """Scraper for GA tax-foreclosure notices from georgiapublicnotice.com."""

    SOURCE_NAME = "ganotices"
    BASE_URL = GAFORECLOSURES_BASE_URL

    def __init__(
        self,
        search_type: str = "foreclosure",
        delay: float = 1.5,
        use_proxy: bool = True,
        solve_captcha: bool = True,
    ):
        super().__init__(search_type=search_type, delay=delay,
                         use_proxy=use_proxy, solve_captcha=solve_captcha)

    def _get_target_counties(self) -> set[str]:
        return COUNTY_SET

    @staticmethod
    def _is_mortgage_foreclosure(text: str) -> bool:
        """True only when *text* is clearly a mortgage/deed-of-trust (bank) sale.

        Used as a cheap grid-level pre-filter so we don't spend a Turnstile
        solve on notices that are obviously not tax foreclosures. Ambiguous
        notices (no clear signal either way) are NOT flagged here -- they are
        passed through to the detail-level check.
        """
        if not text:
            return False
        low = text.lower()
        has_mortgage = any(re.search(p, low) for p in _MORTGAGE_FC_PATTERNS)
        has_tax = any(re.search(p, low) for p in _TAX_FC_PATTERNS)
        return has_mortgage and not has_tax

    @staticmethod
    def _is_quiet_title(text: str) -> bool:
        """True when *text* is a quiet-title / tax-redemption title action
        rather than an upcoming tax-sale foreclosure. We only want the latter.
        """
        if not text:
            return False
        low = text.lower()
        return any(re.search(p, low) for p in _QUIET_TITLE_PATTERNS)

    @staticmethod
    def _is_post_sale(text: str) -> bool:
        """True when *text* is a post-tax-sale proceeding (excess/surplus-fund
        interpleader, foreclosure of the equity of redemption, tax-sale deed)
        rather than an upcoming tax-sale foreclosure.
        """
        if not text:
            return False
        low = text.lower()
        return any(re.search(p, low) for p in _POST_SALE_PATTERNS)

    @staticmethod
    def _extract_case_number(text: str) -> Optional[str]:
        """Extract the civil / file / case action number from a notice body."""
        if not text:
            return None
        m = _CASE_NO_RE.search(text)
        return m.group(1) if m else None

    @staticmethod
    def _is_tax_foreclosure(text: str) -> bool:
        """Authoritative check: is *text* a tax foreclosure / tax sale notice?

        Keeps only county-trustee sales for delinquent property taxes
        (T.C.A. ch. 67). A deed-of-trust / substitute-trustee bank sale is
        rejected unless it also carries an explicit tax-sale signal. Quiet-
        title / tax-redemption proceedings are rejected outright.
        """
        if not text:
            return False
        low = text.lower()
        if GanoticesScraper._is_quiet_title(low):
            return False
        if GanoticesScraper._is_post_sale(low):
            return False
        has_mortgage = any(re.search(p, low) for p in _MORTGAGE_FC_PATTERNS)
        has_tax = any(re.search(p, low) for p in _TAX_FC_PATTERNS)
        if has_mortgage and not has_tax:
            return False
        return has_tax

    def run(self) -> list[PropertyData]:
        """Override base filter: keep notices with UNKNOWN acreage.

        TN foreclosure notices frequently omit an explicit acreage figure.
        Dropping them (base behaviour treats None as below the threshold)
        would discard valid leads before GIS enrichment can supply acreage.
        """
        if not config.TWO_CAPTCHA_API_KEY:
            print(
                f"\n{'!' * 70}\n"
                f"  FATAL: TWO_CAPTCHA_API_KEY is not set.\n"
                f"  Set it in your .env file or as an environment variable.\n"
                f"{'!' * 70}\n",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)

        state_counties = self._get_target_counties()
        count = len(state_counties)
        print(f"\n{'='*60}")
        print(f"  Running {self.SOURCE_NAME} scraper")
        print(f"  Target: {count} counties (keeping unknown-acreage notices)")
        print(f"{'='*60}")

        try:
            properties = self.scrape()
            print(f"\n  Total found: {len(properties)}")

            filtered = []
            skipped = 0
            for prop in properties:
                county = (prop.get("county") or "").lower().strip()
                acres = prop.get("acres")
                if county and county in state_counties:
                    if acres is None or acres >= config.MIN_ACRES:
                        prop["county"] = county.title()
                        filtered.append(prop)
                    else:
                        skipped += 1
                else:
                    skipped += 1

            print(f"  After filtering: {len(filtered)} qualifying, {skipped} skipped")
            return filtered
        except Exception as e:
            logger.error("Scraper %s failed: %s", self.SOURCE_NAME, e, exc_info=True)
            return []

    def scrape(self):  # -> List[PropertyData]
        """Run the scraper: search georgiapublicnotice.com and extract qualifying cases."""
        print(f"\n  GA FORECLOSURES - {len(COUNTY_SET)} target counties")
        print(f"  Search type: {self.search_type}")
        print(f"  Proxy: {'enabled' if self.use_proxy else 'disabled'}")
        print(f"  Captcha solving: {'enabled' if self.solve_captcha else 'disabled (search only)'}")
        print()

        properties = []

        proxy_cfg = {"server": config.PROXY_URL} if self.use_proxy and config.PROXY_URL else None

        from .base import camoufox_context
        with camoufox_context(proxy=proxy_cfg) as page:
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })

            try:
                print("  [1/4] Connecting to georgiapublicnotice.com ...", end=" ", flush=True)
                page.goto(self.BASE_URL + "/", wait_until="domcontentloaded", timeout=30000)
                session_id = self._extract_session(page.url)
                if not session_id:
                    logger.error("Could not extract session ID")
                    return []
                print(f"session={session_id}")

                categories = [c.strip() for c in GAFORECLOSURES_CATEGORIES.split(",") if c.strip()]
                print(f"  [2/4] Searching GA mountain-county sales across {len(categories)} categories: {categories}")
                all_records = []
                for category in categories:
                    self._select_category(page, category)
                    for county in sorted(COUNTY_SET):
                        old_pks = self._grid_pks(page)
                        self._select_county(page, county)
                        self._submit_search(page)
                        self._wait_grid_refresh(page, old_pks)

                        recs = self._parse_grid_records(page)
                        for r in recs:
                            r["county"] = county
                            r["category"] = category

                        # Paginate within the county (GridView pager).
                        seen_pk = set(r["pk_id"] for r in recs)
                        info = self._page_info(page)
                        if info:
                            cur, total = info["cur"], info["total"]
                            while cur < total:
                                self._goto_next_page(page, cur + 1)
                                more = self._parse_grid_records(page)
                                for r in more:
                                    if r["pk_id"] in seen_pk:
                                        continue
                                    seen_pk.add(r["pk_id"])
                                    r["county"] = county
                                    r["category"] = category
                                    recs.append(r)
                                nxt = self._page_info(page)
                                if not nxt:
                                    break
                                cur, total = nxt["cur"], nxt["total"]
                        all_records.extend(recs)
                        print(f"    [{category}] {county}: {len(recs)} notices")

                records = all_records
                print("  [3/4] Parsing results ...")
                print(f"  Found {len(records)} total notices")

                target_records = [r for r in records if (r.get("county") or "").lower() in COUNTY_SET]
                print(f"  {len(target_records)} in target counties")

                # Pre-filter: drop notices that are quiet-title / tax-redemption
                # title actions or post-tax-sale proceedings (excess-fund
                # interpleaders, equity of redemption) -- not actual sales.
                # Mortgage / lender "Sheriff's Sales" are now KEPT (broadened
                # scope), since those are genuine foreclosure sales.
                pre_filtered = []
                quiet_skipped = 0
                postsale_skipped = 0
                for r in target_records:
                    full = r.get("full_text") or ""
                    if self._is_quiet_title(full):
                        quiet_skipped += 1
                    elif self._is_post_sale(full):
                        postsale_skipped += 1
                    else:
                        pre_filtered.append(r)
                target_records = pre_filtered
                print(f"  {quiet_skipped} quiet-title; {postsale_skipped} post-sale dropped; "
                      f"{len(target_records)} foreclosure-sale candidates remain")

                print(f"  [4/4] Extracting details ({len(target_records)} cases) ...")
                for i, rec in enumerate(target_records):
                    print(f"    [{i+1}/{len(target_records)}] {rec['sp_case'] or rec['pk_id']} - {rec.get('county', '?')}",
                          end=" ", flush=True)
                    try:
                        props = self._extract_detail(page, session_id, rec)
                    except Exception as e:
                        logger.warning("Detail extraction crashed for %s: %s", rec.get("pk_id"), e)
                        props = []
                    if props:
                        print(f"-> {len(props)} qualifying parcel(s)")
                        properties.extend(props)
                    else:
                        print("(skipped)")

            finally:
                pass

        return properties

    # ---- browser interactions ---------------------------------------------

    # ---- browser interactions ---------------------------------------------

    def _select_category(self, page, category: str) -> None:
        """Select a popular-search category (triggers an auto-postback)."""
        page.select_option(
            'select[id*="ddlPopularSearches"]',
            category,
        )
        page.wait_for_timeout(5000)
        # Wait for the county checkbox list to re-render after the postback.
        for _ in range(10):
            try:
                page.wait_for_selector('input[id*="lstCounty"]', state="attached", timeout=3000)
                break
            except Exception:
                page.wait_for_timeout(1000)

    def _select_county(self, page, county: str) -> None:
        """Check a single county checkbox.

        ASP.NET's CheckBoxList auto-postbacks on each check and only retains the
        most-recently-clicked county, so we search one county at a time (the
        caller loops over all target counties).
        """
        page.evaluate(
            """(c) => {
                const boxes = Array.from(document.querySelectorAll('input[id*="lstCounty"]'));
                for (const b of boxes) {
                    const lbl = b.closest('label') || b.parentElement;
                    const t = (lbl ? lbl.innerText : '').trim().toLowerCase();
                    if (t === c) { b.click(); return; }
                }
            }""",
            county.lower(),
        )
        page.wait_for_timeout(3000)

    def _submit_search(self, page) -> None:
        """Click the search ("Go") button to run the filtered query."""
        page.evaluate(
            "() => { const b = document.getElementById("
            "'ctl00_ContentPlaceHolder1_as1_btnGo1'); if (b) b.click(); }"
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        try:
            page.wait_for_selector('input[id*="hdnPKValue"]', state="attached", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

    def _page_info(self, page):
        """Return {'cur':int,'total':int} parsed from the GridView pager, or None."""
        return page.evaluate(
            """() => {
                const tds = Array.from(document.querySelectorAll('td'));
                const p = tds.map(td => (td.innerText||'').trim())
                             .find(t => /Page\\s+\\d+\\s+of\\s+\\d+/i.test(t));
                if (!p) return null;
                const m = p.match(/Page\\s+(\\d+)\\s+of\\s+(\\d+)/i);
                return m ? {cur: parseInt(m[1],10), total: parseInt(m[2],10)} : null;
            }"""
        )

    def _grid_pks(self, page):
        """Current GridView primary-key values (used to detect a refresh)."""
        try:
            return page.evaluate(
                "() => Array.from(document.querySelectorAll('input[id*=\"hdnPKValue\"]')).map(r => r.value)"
            )
        except Exception:
            return []

    def _wait_grid_refresh(self, page, old_pks, timeout: int = 30000) -> None:
        """Wait until the GridView rows change from ``old_pks``.

        The search/county postbacks are AJAX (UpdatePanel) updates, so waiting
        on ``load``/``networkidle`` or on the row marker selector is not enough
        -- the old rows are still present until the new response renders. We
        poll until the rendered PK set differs (or the grid goes empty).
        """
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass
        old = set(old_pks)
        for _ in range(int(timeout / 1000)):
            try:
                cur = set(self._grid_pks(page))
            except Exception:
                cur = old
            if cur != old:
                return
            page.wait_for_timeout(1000)

    def _goto_next_page(self, page, next_num: int) -> bool:
        """Click the pager link for page ``next_num``. Returns True if clicked."""
        old_pks = self._grid_pks(page)
        ok = page.evaluate(
            """(n) => {
                const links = Array.from(document.querySelectorAll('td a, td span'));
                for (const a of links) {
                    if ((a.innerText||'').trim() === String(n)) { a.click(); return true; }
                }
                for (const a of links) {
                    const t = (a.innerText||'').trim().toLowerCase();
                    if (t === 'next' || t === '>' || t === '»' || t === '...') { a.click(); return true; }
                }
                return false;
            }""",
            next_num,
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        try:
            page.wait_for_selector('input[id*="hdnPKValue"]', state="attached", timeout=20000)
        except Exception:
            pass
        self._wait_grid_refresh(page, old_pks)
        return bool(ok)

    def _parse_grid_records(self, page):
        """Parse ASP.NET GridView rows into record dicts (retry on navigation)."""
        last_err = None
        for _ in range(4):
            try:
                return self._parse_grid_records_once(page)
            except Exception as e:  # page navigated mid-evaluate
                last_err = e
                page.wait_for_timeout(2500)
        logger.warning("Grid parse failed after retries: %s", last_err)
        return []

    def _parse_grid_records_once(self, page):
        records = page.evaluate("""() => {
            const table = document.querySelector('table[id*="GridView"]');
            if (!table) return [];

            const allRows = Array.from(table.querySelectorAll('tr'));
            const seenPk = new Set();
            const results = [];

            allRows.forEach(function(row) {
                const hdn = row.querySelector('input[id*="hdnPKValue"]');
                if (!hdn) return;
                const pkId = hdn.value;
                if (seenPk.has(pkId)) return;
                seenPk.add(pkId);

                let texts = [row.textContent || ''];
                let sib = row.nextElementSibling;
                while (sib && sib.tagName === 'TR' && !sib.querySelector('input[id*="hdnPKValue"]')) {
                    texts.push(sib.textContent || '');
                    sib = sib.nextElementSibling;
                }
                const fullText = texts.join(' ').replace(/\\s+/g, ' ').trim();

                const spMatch = fullText.match(/\\b(\\d+SP\\d+[-\\w]*)\\b/);
                const spCase = spMatch ? spMatch[1] : null;

                let county = null;
                const cm1 = fullText.match(/STATE OF GEORGIA,?\\s*COUNTY OF\\s+([A-Z][A-Za-z]+)/i);
                if (cm1) county = cm1[1].trim();
                if (!county) {
                    const cm2 = fullText.match(/([A-Z][A-Za-z]+)\\s+COUNTY,?\\s*GEORGIA/i);
                    if (cm2) county = cm2[1].trim();
                }
                if (!county) {
                    const cm3 = fullText.match(/COUNTY OF\\s+([A-Z][A-Za-z]+)/i);
                    if (cm3) county = cm3[1].trim();
                }
                if (!county) {
                    const cm4 = fullText.match(/([A-Z][A-Za-z]+)\\s+County\\s*Courthouse/i);
                    if (cm4) county = cm4[1].trim();
                }

                const btn2 = row.querySelector('input[id*="btnView2"]');
                let detailUrl = null;
                if (btn2) {
                    const onclick = btn2.getAttribute('onclick') || '';
                    const urlMatch = onclick.match(/location\\.href='([^']+)'/);
                    if (urlMatch) detailUrl = urlMatch[1];
                }

                results.push({
                    pk_id: pkId,
                    sp_case: spCase,
                    county: county,
                    detail_url: detailUrl,
                    full_text: fullText,
                });
            });
            return results;
        }""")
        return records  # List[dict]

    @staticmethod
    def _parse_parcels(text: str, county: str, auction_date: Optional[str],
                       detail_url: Optional[str]) -> list[PropertyData]:
        """Split a bundled GA tax-sale notice into one record per parcel.

        White County posts many parcels under a single notice ("multiple
        listings by deed/page"); Towns County likewise lists each tax map &
        parcel as a separate block. Each block is its own listing, keyed on
        ``<county>:<parcel_number>`` so the same parcel across duplicate
        postings collapses but distinct parcels do not.
        """
        county = (county or "").lower().strip()
        matches = list(_PARCEL_SPLIT_RE.finditer(text))
        if not matches:
            return []
        parcels: list[PropertyData] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[start:end]
            parcel_no = m.group(1).strip()

            addr_m = _GA_PARCEL_ADDR_RE.search(block)
            address = addr_m.group(1).strip() if addr_m else None
            if not address:
                address = f"Parcel {parcel_no}"
            acres_m = _PARCEL_ACRES_RE.search(block)
            acres = float(acres_m.group(1)) if acres_m else None

            desc = block.strip()
            parcel: PropertyData = {
                "source": "ganotices",
                "source_listing_id": f"{county}:{parcel_no}",
                "url": detail_url,
                "address": address[:120] if address else None,
                "city": None,
                "county": county,
                "state": "GA",
                "zip_code": None,
                "latitude": None,
                "longitude": None,
                "price": 1,
                "acres": acres,
                "description": desc,
                "property_type": "tax_foreclosure",
                "image_url": None,
                "court_case": None,
                "auction_date": auction_date,
                "parcel_number": parcel_no,
                "raw_source_text": desc,
                "raw_paragraph": desc,
            }
            parcels.append(parcel)
        return parcels

    def _extract_detail(self, page, session_id: str, record: dict) -> list[PropertyData]:
        """Navigate to detail page, pass the Turnstile gate, extract notice text.

        Returns a LIST of per-parcel PropertyData records (a single GA notice
        may bundle many parcels), or an empty list if the notice is rejected
        (too short, quiet-title / post-sale, non-tax foreclosure, or no
        parseable parcels).
        """
        pk_id = record["pk_id"]
        detail_url = f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}"

        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning("Failed to load detail page: %s", e)
            return []

        page.wait_for_timeout(3000)

        # Pass the Cloudflare Turnstile gate if it is present.
        if page.query_selector(TURNSTILE_FIELD):
            token = self._solve_turnstile(page.url, GAFORECLOSURES_TURNSTILE_SITE_KEY)
            if not token:
                logger.warning("Turnstile solve failed for %s", pk_id)
                return []
            self._inject_turnstile_token(page, token)
            page.wait_for_timeout(800)
            # Submit via the ASP.NET postback directly — the View Notice button
            # stays hidden until the widget "verifies", so a real click times out.
            try:
                page.evaluate(
                    "() => __doPostBack("
                    "'ctl00$ContentPlaceHolder1$PublicNoticeDetailsBody1$btnViewNotice', '')"
                )
            except Exception as e:
                logger.warning("btnViewNotice submit failed for %s: %s", pk_id, e)
                return []
            try:
                page.wait_for_load_state("load", timeout=60000)
            except Exception:
                pass

        # Wait for the notice body to render (past the challenge page).
        notice_text = ""
        for _ in range(24):
            notice_text = page.evaluate(
                "() => (document.body ? document.body.innerText : '') || ''"
            )
            blocked = page.evaluate(
                "() => { const el = document.getElementById("
                "'ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_lblMessage');"
                " return el ? (el.textContent || '').toLowerCase().includes('complete the challenge') : false; }"
            )
            if not blocked and len(notice_text) > 1500:
                break
            page.wait_for_timeout(2500)

        if len(notice_text) < 500:
            logger.warning("Content too short (%d chars) for %s", len(notice_text), pk_id)
            return []

        # Trim to the notice body if a marker is present.
        if "Notice Content" in notice_text:
            notice_text = notice_text.split("Notice Content", 1)[1]
        for marker in ("Powered by Translate", "Copyright ©"):
            idx = notice_text.find(marker)
            if idx > 0:
                notice_text = notice_text[:idx]
        notice_text = notice_text.strip()

        # Use the PDF's full text as the canonical source when available;
        # fall back to the (truncated) on-page notice text otherwise.
        pdf_text = self._fetch_pdf_text(page, pk_id)
        raw_text = pdf_text or notice_text

        # Authoritative classification on the full notice text. Quiet-title /
        # tax-redemption title actions and post-sale proceedings are rejected
        # (not actual sales). We keep tax-sale foreclosures AND mortgage /
        # lender "Sheriff's Sales" (broadened scope).
        if self._is_quiet_title(raw_text):
            logger.info("Dropping quiet-title/tax-redemption %s", pk_id)
            return []
        if self._is_post_sale(raw_text):
            logger.info("Dropping post-sale proceeding %s", pk_id)
            return []
        if not (self._is_tax_foreclosure(raw_text) or self._is_mortgage_foreclosure(raw_text)):
            logger.info("Dropping non-foreclosure %s", pk_id)
            return []

        auction_date = self._extract_sale_date(raw_text)
        county = (record.get("county") or "").lower().strip()
        parcels = self._parse_parcels(raw_text, county, auction_date, detail_url)
        if not parcels:
            logger.warning("No parcels parsed for %s (county=%s)", pk_id, county)
        return parcels

    def _fetch_pdf_text(self, page, notice_id: str) -> Optional[str]:
        """Download and parse the notice PDF if a download link exists.

        Returns the extracted PDF text. The on-page HTML notice is an OCR
        conversion that is truncated, so the PDF text is used as the
        canonical (full) source text whenever a PDF is available.
        """
        el = page.query_selector(DOWNLOAD_LINK)
        if not el:
            return None
        try:
            href = el.get_attribute("href")
        except Exception:
            return None
        if not href:
            return None
        href = html_lib.unescape(href)
        try:
            resp = page.request.get(href, timeout=60000)
            data = resp.body()
        except Exception as e:
            logger.warning("PDF download failed for %s: %s", notice_id, e)
            return None
        text = _extract_pdf_text(data)
        if text:
            logger.debug("PDF for %s: %d chars", notice_id, len(text))
        return text

    @staticmethod
    def _extract_address(text: str) -> Optional[str]:
        """Extract the first street address found in a notice body."""
        if not text:
            return None
        m = _ADDRESS_RE.search(text)
        if not m:
            return None
        addr = m.group(1).strip()
        # Collapse whitespace / tighten the trailing type token
        addr = re.sub(r"\s+", " ", addr)
        return addr[:120] if addr else None

    @staticmethod
    def _extract_ga_address(text: str) -> Optional[str]:
        """Extract the first *parcel* address from a GA sheriff's-sale notice.

        The notice preamble lists the Tax Commissioner's office / counsel
        addresses (the sale *location*, not a property for sale), so we anchor
        on the ``known as`` / ``located on`` phrasing that precedes each
        parcel's street address. When only a parcel number is given (Towns
        County style), we fall back to that.
        """
        if not text:
            return None
        body = text.split("File #:", 1)[1] if "File #:" in text else text
        m = _GA_PARCEL_ADDR_RE.search(body)
        if m:
            return m.group(1).strip()[:120]
        m2 = _GA_PARCEL_NO_RE.search(text)
        if m2:
            return f"Parcel {m2.group(1)}"
        return None

    @staticmethod
    def _extract_sale_date(text: str) -> Optional[str]:
        """Extract the GA tax-sale date (ISO `YYYY-MM-DD`).

        GA tax sales are held on the **first Tuesday of the month**; the
        notice phrases this as "first Tuesday in <Month> <Year>". We compute
        that calendar date directly rather than trusting the "the same being
        <date>" gloss, which is sometimes wrong or missing.
        """
        if not text:
            return None
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
            "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
            "november": 11, "december": 12,
        }
        ft = _FIRST_TUESDAY_RE.search(text)
        if ft:
            try:
                y, mo = int(ft.group(2)), months[ft.group(1).lower()]
                first = date(y, mo, 1)
                # Monday=0 ... Tuesday=1 => offset to first Tuesday
                offset = (1 - first.weekday()) % 7
                return (first + timedelta(days=offset)).isoformat()
            except (ValueError, KeyError):
                pass
        m = _SALE_DATE_RE.search(text)
        if not m:
            return None
        try:
            return date(int(m.group(3)), months[m.group(1).lower()], int(m.group(2))).isoformat()
        except (ValueError, KeyError):
            return None


def _extract_pdf_text(data: bytes) -> Optional[str]:
    """Extract text from PDF bytes via pdfplumber."""
    if not data or data[:4] != b"%PDF":
        return None
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed; cannot parse PDF")
        return None
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages).strip()
        return text or None
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return None


def scrape_with_enrichment(
    solve_captcha: bool = True,
    enrich: bool = True,
) -> list[PropertyData]:
    """Run the GA tax-foreclosure scraper.

    GA has no statewide parcel/GIS enrichment source (per project notes), so
    ``enrich`` is accepted for API parity but no map enrichment is performed.
    """
    scraper = GanoticesScraper(solve_captcha=solve_captcha)
    properties = scraper.run()
    return properties
