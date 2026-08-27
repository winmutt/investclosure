"""TN Foreclosure Notices scraper — tnpublicnotice.com via Playwright + 2captcha.

Mirrors ncforeclosures.py architecture for the Tennessee Press Association site.
Scrapes foreclosure public notices for 95 TN counties.
"""
from __future__ import annotations
import html as html_lib
import io
import re
import sys
import datetime
import hashlib
import logging
from typing import Optional

from .base import BaseForeclosureScraper, PropertyData
from .config import (
    config,
    TNFORECLOSURES_BASE_URL,
    TNFORECLOSURES_TURNSTILE_SITE_KEY,
    TNFORECLOSURES_POPULAR_SEARCH_VALUE,
    TN_FORECLOSURE_COUNTIES,
)

logger = logging.getLogger(__name__)

COUNTY_SET = set(TN_FORECLOSURE_COUNTIES)

# Only process notices published within this many days (the grid is sorted by
# publication date, newest first, so we paginate until we pass the cutoff).
LOOKBACK_DAYS = 61

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

_DATE_RE = re.compile(
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*)?"
    r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})"
)


def _parse_notice_date(text: str):
    """Extract the leading publication date (e.g. 'Tuesday, August 25, 2026')."""
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).title())
    if not mon:
        return None
    try:
        return datetime.date(int(m.group(3)), mon, int(m.group(2)))
    except Exception:
        return None

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

# tnpublicnotice.com files mortgage/deed-of-trust (bank) foreclosures in the
# same "Foreclosures" / "Tax Sales" popular searches as genuine tax
# foreclosures. We must keep ONLY tax foreclosures (county trustee sales for
# delinquent property taxes) and drop mortgage/bank ones.
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


class TNForeclosureScraper(BaseForeclosureScraper):
    """Scraper for TN public foreclosure notices from tnpublicnotice.com."""

    SOURCE_NAME = "tnforeclosures"
    BASE_URL = TNFORECLOSURES_BASE_URL

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
    def _is_tax_foreclosure(text: str) -> bool:
        """Authoritative check: is *text* a tax foreclosure / tax sale notice?

        Keeps only county-trustee sales for delinquent property taxes
        (T.C.A. ch. 67). A deed-of-trust / substitute-trustee bank sale is
        rejected unless it also carries an explicit tax-sale signal.
        """
        if not text:
            return False
        low = text.lower()
        has_mortgage = any(re.search(p, low) for p in _MORTGAGE_FC_PATTERNS)
        has_tax = any(re.search(p, low) for p in _TAX_FC_PATTERNS)
        if has_mortgage and not has_tax:
            return False
        return has_tax

    @staticmethod
    def _is_publication_notice(text: str) -> bool:
        """Skip court *service* publications that are not parcel sales.

        tnpublicnotice.com mixes in "NOTICE OF PUBLICATION" filings used to
        serve non-resident / cannot-be-located defendants (e.g. consolidated
        delinquent-taxpayer lists naming dozens of parties). These have no
        single street address or auction and must not become "properties".
        Genuine tax *sales* are titled "NOTICE OF ... TAX SALE" / "NOTICE OF
        SALE", never "NOTICE OF PUBLICATION", so this is a safe discriminator.
        """
        if not text:
            return False
        t = text.upper()
        if "NOTICE OF PUBLICATION" not in t:
            return False
        return any(
            k in t
            for k in (
                "NON-RESIDENT", "CANNOT BE LOCATED", "RETURN OF PROCESS",
                "SERVICE OF PROCESS",
            )
        )

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
        """Run the scraper: search tnpublicnotice.com and extract qualifying cases."""
        print(f"\n  TN FORECLOSURES - {len(COUNTY_SET)} target counties")
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
                print("  [1/4] Connecting to tnpublicnotice.com ...", end=" ", flush=True)
                page.goto(self.BASE_URL + "/", wait_until="networkidle", timeout=30000)
                session_id = self._extract_session(page.url)
                if not session_id:
                    logger.error("Could not extract session ID")
                    return []
                print(f"session={session_id}")

                print("  [2/4] Searching foreclosure notices ...", end=" ", flush=True)
                self._search_foreclosures(page)
                print("done")

                print("  [3/4] Parsing results ...")
                cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
                print(f"  Recency cutoff: {cutoff.isoformat()} (last {LOOKBACK_DAYS} days)")

                all_records = []
                seen_pk = set()

                def _collect(recs):
                    stop = False
                    for r in recs:
                        pk = r.get("pk_id")
                        if pk in seen_pk:
                            continue
                        seen_pk.add(pk)
                        d = _parse_notice_date(r.get("full_text") or "")
                        if d is not None and d < cutoff:
                            # Grid is sorted newest-first; everything after is older.
                            stop = True
                            break
                        all_records.append(r)
                    return stop

                # Page 1
                stop = _collect(self._parse_grid_records(page))
                print(f"  Page 1: {len(all_records)} kept (last {LOOKBACK_DAYS} days)")

                # Paginate through subsequent pages until we pass the cutoff.
                info = self._page_info(page)
                page_no = 1
                if info:
                    cur, total = info["cur"], info["total"]
                    # Hard cap (defensive): a pager that fails to advance must
                    # not loop forever. The 2-month window is never > ~10 pages.
                    while cur < total and not stop and page_no < 50:
                        self._goto_next_page(page, cur + 1)
                        page_no += 1
                        before = len(all_records)
                        stop = _collect(self._parse_grid_records(page))
                        print(f"  Page {page_no}: +{len(all_records) - before} kept "
                              f"(total {len(all_records)})")
                        nxt = self._page_info(page)
                        if not nxt:
                            break
                        cur, total = nxt["cur"], nxt["total"]

                records = all_records
                print(f"  Found {len(records)} notices in last {LOOKBACK_DAYS} days")

                target_records = [r for r in records if (r.get("county") or "").lower() in COUNTY_SET]
                print(f"  {len(target_records)} in target counties")

                # Pre-filter: drop notices that are clearly mortgage/deed-of-trust
                # (bank) foreclosures, and court *service* publications (e.g.
                # consolidated delinquent-taxpayer lists) that are not parcel
                # sales, so we don't burn a Turnstile solve on them.
                pre_filtered = []
                mortgage_skipped = 0
                publication_skipped = 0
                for r in target_records:
                    if self._is_mortgage_foreclosure(r.get("full_text") or ""):
                        mortgage_skipped += 1
                    elif self._is_publication_notice(r.get("full_text") or ""):
                        publication_skipped += 1
                    else:
                        pre_filtered.append(r)
                target_records = pre_filtered
                print(f"  {mortgage_skipped} dropped as mortgage/bank foreclosures; "
                      f"{publication_skipped} dropped as court publications; "
                      f"{len(target_records)} tax-candidate notices remain")

                print(f"  [4/4] Extracting details ({len(target_records)} cases) ...")
                for i, rec in enumerate(target_records):
                    print(f"    [{i+1}/{len(target_records)}] {rec['sp_case'] or rec['pk_id']} - {rec.get('county', '?')}",
                          end=" ", flush=True)
                    prop = self._extract_detail(page, session_id, rec)
                    if prop:
                        print("-> qualifying property")
                        properties.append(prop)
                    else:
                        print("(skipped)")

            finally:
                pass

        # Within-run dedup: tnpublicnotice.com lists the same consolidated
        # notice under several distinct grid row ids (e.g. a delinquent-
        # taxpayer publication re-shown per defendant segment). Collapse those
        # to a single property by notice content so we never insert N copies
        # of one notice within a single run.
        seen_content = set()
        deduped = []
        for p in properties:
            text = (p.get("raw_source_text") or p.get("description") or "")
            if text:
                key = hashlib.md5(text.lower().encode("utf-8")).hexdigest()
            else:
                # No text to key on — fall back to location so we don't
                # accidentally merge genuinely different address-less notices.
                key = ("loc", p.get("county") or "", p.get("state") or "")
            if key in seen_content:
                continue
            seen_content.add(key)
            deduped.append(p)
        if len(deduped) != len(properties):
            print(f"  Within-run dedup: dropped {len(properties) - len(deduped)} "
                  f"duplicate notice(s)")
        properties = deduped

        return properties

    # ---- browser interactions ---------------------------------------------

    def _search_foreclosures(self, page) -> None:
        page.select_option(
            'select[name="ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"]',
            TNFORECLOSURES_POPULAR_SEARCH_VALUE,
        )
        page.wait_for_timeout(8000)
        # The results grid paginates at 10/page by default; raise it so all
        # foreclosure notices (statewide) load in a single page for parsing.
        try:
            page.select_option(PER_PAGE_SELECT, "50")
            page.wait_for_timeout(4000)
        except Exception as e:
            logger.warning("Could not raise per-page count: %s", e)

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
            page.wait_for_load_state("networkidle", timeout=timeout)
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

    def _goto_next_page(self, page, next_num: int) -> bool:
        """Advance the GridView pager to the next page. Returns True if advanced.

        The ``WSExtendedGridNP`` pager exposes only First/Prev/Next/Last image
        buttons (no numeric links); their ``onclick`` is just a scroll helper,
        so a normal/real click is swallowed and the page never changes. The
        reliable path is to invoke the ASP.NET ``__doPostBack`` for the
        GridView's ``Page$Next`` command -- exactly what the Next button does
        server-side. ``next_num`` is only used by the numeric-link fallback.
        """
        old_pks = self._grid_pks(page)
        # ASP.NET posts back using the control's UniqueID, which is exposed in
        # the element's *name* attribute ($-separated) -- NOT the id property
        # (which is normalized to underscores and does not match server state).
        btn_id = page.evaluate(
            """() => {
                const b = document.querySelector('input[id*="btnNext"]');
                return b ? (b.getAttribute('name') || b.name) : null;
            }"""
        )
        if not btn_id:
            # Fallback for pagers that expose numeric <a> links.
            try:
                loc = page.get_by_role("link", name=str(next_num), exact=True)
                if loc.count() == 0:
                    loc = page.get_by_text(str(next_num), exact=True)
                loc.first.click(timeout=8000)
            except Exception:
                return False
        else:
            try:
                # Target the Next button's own id (server-side event target).
                page.evaluate("(btnId) => __doPostBack(btnId, '')", btn_id)
            except Exception as e:
                logger.warning("__doPostBack(next) failed: %s", e)
                return False
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        try:
            page.wait_for_selector('input[id*="hdnPKValue"]', state="attached", timeout=20000)
        except Exception:
            pass
        self._wait_grid_refresh(page, old_pks)
        return True

    def _parse_grid_records(self, page):
        """Parse ASP.NET GridView rows into record dicts."""
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
                const cm1 = fullText.match(/TENNESSEE\\s*[,:]?\\s*([A-Z][A-Za-z]+)\\s+COUNTY/i);
                if (cm1) county = cm1[1].trim();
                if (!county) {
                    const cm2 = fullText.match(/([A-Z][A-Za-z]+)\\s+COUNTY\\s*[,:]?\\s*TENNESSEE/i);
                    if (cm2) county = cm2[1].trim();
                }
                if (!county) {
                    const cm3 = fullText.match(/(?:at\\s+the\\s+\\w+\\s+(?:door|entrance|breezeway|steps|lobby)[^,]*,\\s*)([A-Z][A-Za-z]+)\\s+County\\s*Courthouse/i);
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

    def _extract_detail(self, page, session_id: str, record: dict) -> Optional[PropertyData]:
        """Navigate to detail page, pass the Turnstile gate, extract notice text."""
        pk_id = record["pk_id"]
        detail_url = f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}"

        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning("Failed to load detail page: %s", e)
            return None

        page.wait_for_timeout(3000)

        # Pass the Cloudflare Turnstile gate if it is present.
        if page.query_selector(TURNSTILE_FIELD):
            token = self._solve_turnstile(page.url, TNFORECLOSURES_TURNSTILE_SITE_KEY)
            if not token:
                logger.warning("Turnstile solve failed for %s", pk_id)
                return None
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
                return None
            try:
                page.wait_for_load_state("load", timeout=60000)
            except Exception:
                pass

        # Wait for the notice body to render (past the challenge page).
        notice_text = ""
        for _ in range(24):
            notice_text = page.evaluate("() => document.body.innerText || ''")
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
            return None

        # Trim to the notice body if a marker is present.
        if "Notice Content" in notice_text:
            notice_text = notice_text.split("Notice Content", 1)[1]
        for marker in ("Powered by Translate", "Copyright ©"):
            idx = notice_text.find(marker)
            if idx > 0:
                notice_text = notice_text[:idx]
        notice_text = notice_text.strip()

        acres = self._extract_acreage(notice_text)
        # Keep records whose acreage is unknown (None); only drop when an
        # explicit acreage is present and below the minimum threshold.
        if acres is not None and acres < config.MIN_ACRES:
            return None

        address = self._extract_address(notice_text)

        # Use the PDF's full text as the canonical source when available;
        # fall back to the (truncated) on-page notice text otherwise.
        pdf_text = self._fetch_pdf_text(page, pk_id)
        raw_text = pdf_text or notice_text

        # Authoritative tax-foreclosure check on the full notice text. Mortgage
        # / deed-of-trust (bank) foreclosures are rejected here even if they
        # slipped past the grid pre-filter.
        if not self._is_tax_foreclosure(raw_text):
            logger.info("Dropping non-tax foreclosure %s (mortgage/bank)", pk_id)
            return None

        prop: PropertyData = {
            "source": self.SOURCE_NAME,
            "source_listing_id": record.get("sp_case") or pk_id,
            "url": detail_url,
            "address": address,
            "city": None,
            "county": (record.get("county") or "").lower().strip(),
            "state": "TN",
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": 1,
            "acres": acres,
            "description": raw_text[:2000],
            "property_type": "tax_foreclosure",
            "image_url": None,
            "raw_source_text": raw_text,
            "raw_paragraph": raw_text,
        }
        return prop

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
        # Reject false matches against court-house / tribunal names (e.g.
        # "2026 in the Chancery Court") which are not street addresses.
        if re.search(
            r"\b(chancery|circuit|county|district|superior|federal|probate|"
            r"juvenile|municipal|supreme)\s+court\b",
            addr,
            re.IGNORECASE,
        ):
            return None
        return addr[:120] if addr else None


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
    """Run TN foreclosure scraper with optional TNMap enrichment."""
    from .tnforeclosures import TNForeclosureScraper
    from .tnmap import enrich_with_tnmap

    scraper = TNForeclosureScraper(solve_captcha=solve_captcha)
    properties = scraper.run()

    if enrich and properties:
        # TNMap enrichment is best-effort; a failure there must not discard
        # the properties we already scraped.
        try:
            properties = enrich_with_tnmap(properties)
        except Exception as e:
            logger.warning("TNMap enrichment failed (keeping scraped props): %s", e)

    return properties
