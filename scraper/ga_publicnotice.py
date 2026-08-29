"""GA Public Notice foreclosure scraper — georgiapublicnotice.com (Georgia Press Assoc).

georgiapublicnotice.com is the same "Public Notice" ASP.NET WebForms platform
(shared base in :mod:`scraper.publicnotice_base`) as tnpublicnotice.com /
ncnotices.com. Restricted to the N GA mountain counties: fannin, gilmer,
lumpkin, rabun, towns, union, white. The search is scoped to the "Tax Sales"
popular category so only tax foreclosures are returned -- mortgage/bank
foreclosures, quiet-title / tax-redemption title actions, and post-tax-sale
proceedings (excess-fund interpleaders, equity of redemption) are filtered out.
"""
from __future__ import annotations
import logging
import re
import sys
from datetime import date, timedelta
from typing import List, Optional

from .base import PropertyData
from .config import (
    config,
    GAFORECLOSURES_BASE_URL,
    GAFORECLOSURES_TURNSTILE_SITE_KEY,
    GAFORECLOSURES_CATEGORIES,
    GA_MOUNTAIN_COUNTIES,
)
from .publicnotice_base import (
    LOOKBACK_DAYS,
    PublicNoticeScraper,
    MORTGAGE_FC_PATTERNS,
    TAX_FC_PATTERNS,
    ADDRESS_RE,
    _is_recent_publication,
    extract_street_address,
)

logger = logging.getLogger(__name__)

COUNTY_SET = set(GA_MOUNTAIN_COUNTIES)

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
# "the same being July 7, 2026".
_SALE_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE,
)

# GA tax sales are statutorily held on the FIRST TUESDAY of the month.
_FIRST_TUESDAY_RE = re.compile(
    r"first\s+Tuesday\s+in\s+(January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+(\d{4})",
    re.IGNORECASE,
)

# Split a single (bundled) GA tax-sale notice into its per-parcel blocks.
_PARCEL_SPLIT_RE = re.compile(
    r"(?:File\s+#:\s*\d+\s*Map/Parcel Number:|Map\s*&\s*Parcel:|Map/Parcel Number:)\s*"
    r"(.+?)\s+Defendant",
    re.IGNORECASE,
)
# Acreage within a single parcel block. Two forms:
#   * the legal body -- "containing 1,236.7 acres, more or less" (authoritative)
#   * the parcel-line shorthand -- "063 001 / 1236.70 AC" / "2.07 Acs"
# Both may carry thousands commas, so the captured number is passed through
# ``_normalize_acres`` before use.
_CONTAINING_ACRES_RE = re.compile(
    r"containing\s+([\d,]+(?:\.\d+)?)\s+acres?\b", re.IGNORECASE)
# Parcel-line shorthand (".45 Ac", "1236.70 AC", "2.07 Acs"). The integer part
# is optional so a leading-dot decimal like ".45 Ac" is not misread as 45.
_LINE_ACRES_RE = re.compile(
    r"([\d,]+(?:\.[\d,]+)?|\.\d+)\s*(?:Acs?|AC)\b", re.IGNORECASE)

# Signals that a notice is a QUIET-TITLE / TAX-REDEMPTION title-clearing
# proceeding (post-tax-sale), NOT an upcoming tax-sale foreclosure.
_QUIET_TITLE_PATTERNS = [
    r"petition\s+to\s+establish\s+title",
    r"establish\s+title\s+against\s+all\s+the\s+world",
    r"quiet\s+title",
    r"tax\s+sale\s+redemption",
    r"tax\s+deed\s+redemption",
    r"redeem(?:s|ed|ing)?\s+the\s+tax\s+deed",
    r"order\s+for\s+service\s+by\s+publication",
]
# Post-tax-sale proceedings that are NOT upcoming foreclosure sales.
_POST_SALE_PATTERNS = [
    r"excess\s+funds",
    r"surplus\s+funds",
    r"interpleader",
    r"petition\s+for\s+interpleader",
    r"equity\s+of\s+redemption",
    r"foreclosure\s+of\s+equity",
]

# Civil / file / case action numbers (for de-duplicating same legal case).
_CASE_NO_RE = re.compile(
    r"(?:CIVIL ACTION|FILE|CASE|ACTION)\s+NO\.?\s*([A-Za-z]+\d+)",
    re.IGNORECASE,
)

# County patterns used to pull the *property* county out of a notice body
# (bundled notices may be returned by every county's checkbox search).
_COUNTY_IN_TEXT_PATTERNS = (
    r"COUNTY OF\s+([A-Z][A-Za-z]+)",
    r"([A-Z][A-Za-z]+)\s+County\s*Courthouse",
    r"County:\s*([A-Z][A-Za-z]+)",
    r"([A-Z][A-Za-z]+)\s+County\s*,?\s*(?:Georgia|GA)\b",
    r"STATE OF GEORGIA,?\s*COUNTY OF\s+([A-Z][A-Za-z]+)",
)


class GAPublicNoticeScraper(PublicNoticeScraper):
    """Scraper for GA tax-foreclosure notices from georgiapublicnotice.com."""

    SOURCE_NAME = "ga_publicnotice"
    BASE_URL = GAFORECLOSURES_BASE_URL
    TURNSTILE_SITE_KEY = GAFORECLOSURES_TURNSTILE_SITE_KEY

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

    def _county_from_grid_text(self, full_text: str) -> Optional[str]:
        """Pull the GA property county from a grid-row's text.

        The georgiapublicnotice.com result grid lists each notice with its
        newspaper/county label, typically ``County: <Name>`` on the row. Because
        the site's county checkboxes do not reliably filter by the *property*
        county (selecting "Rabun" can return Lumpkin newspaper notices), we
        trust the county named in the row text rather than the checkbox -- the
        notice's own county label reflects the sheriff's-sale jurisdiction.
        """
        return self._county_from_notice_text(full_text)

    @staticmethod
    def _is_quiet_title(text: str) -> bool:
        if not text:
            return False
        return any(re.search(p, text.lower()) for p in _QUIET_TITLE_PATTERNS)

    @staticmethod
    def _is_post_sale(text: str) -> bool:
        if not text:
            return False
        return any(re.search(p, text.lower()) for p in _POST_SALE_PATTERNS)

    @staticmethod
    def _extract_case_number(text: str) -> Optional[str]:
        if not text:
            return None
        m = _CASE_NO_RE.search(text)
        return m.group(1) if m else None

    @staticmethod
    def _is_tax_foreclosure(text: str) -> bool:
        """Authoritative check: keep only upcoming tax-sale foreclosures.

        Rejects quiet-title / tax-redemption title actions and post-sale
        proceedings outright; then applies the shared tax-vs-mortgage
        classifier (county-trustee sales for delinquent property taxes).
        """
        if not text:
            return False
        low = text.lower()
        if GAPublicNoticeScraper._is_quiet_title(low):
            return False
        if GAPublicNoticeScraper._is_post_sale(low):
            return False
        has_mortgage = any(re.search(p, low) for p in MORTGAGE_FC_PATTERNS)
        has_tax = any(re.search(p, low) for p in TAX_FC_PATTERNS)
        if has_mortgage and not has_tax:
            return False
        return has_tax

    @staticmethod
    def _extract_address(text: str) -> Optional[str]:
        return extract_street_address(text)

    @staticmethod
    def _extract_ga_address(text: str) -> Optional[str]:
        """Extract the first *parcel* address from a GA sheriff's-sale notice.

        The notice preamble lists the Tax Commissioner's office / counsel
        addresses (the sale *location*), so we anchor on the ``known as`` /
        ``located on`` phrasing that precedes each parcel's street address.
        When only a parcel number is given (Towns County style), fall back to it.
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

        GA tax sales are held on the **first Tuesday of the month**; the notice
        phrases this as "first Tuesday in <Month> <Year>". We compute that
        calendar date directly rather than trusting the "the same being <date>"
        gloss, which is sometimes wrong or missing.
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

    @staticmethod
    def _parse_acres(block: str) -> Optional[float]:
        """Parse acreage from a single GA parcel block, handling thousands.

        Prefers the authoritative legal-body "containing X acres" phrase, then
        the parcel-line shorthand ("<n> AC"/"<n> Acs"). Commas are stripped so
        "1,236.7 acres" resolves to 1236.7 rather than a partial 236.7.
        """
        for pat in (_CONTAINING_ACRES_RE, _LINE_ACRES_RE):
            m = pat.search(block)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                if 0.1 < val < 10000:
                    return val
        return None

    @staticmethod
    def _county_from_notice_text(text: str) -> Optional[str]:
        """County named by a GA sheriff's-sale notice / parcel block itself.

        A bundled notice may be returned by every county's checkbox search, so
        the property's true county must come from the notice body (e.g. "lying
        and being in ... Lumpkin County, Georgia") rather than the search box.
        """
        if not text:
            return None
        for pat in _COUNTY_IN_TEXT_PATTERNS:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return None

    @staticmethod
    def _parse_parcels(text: str, county: str, auction_date: Optional[str],
                       detail_url: Optional[str]) -> List[PropertyData]:
        """Split a bundled GA tax-sale notice into one record per parcel.

        Each block is its own listing, keyed on ``<county>:<parcel_number>`` so
        the same parcel across duplicate postings collapses but distinct
        parcels do not. The county is the one the notice body itself names
        (falling back to the grid-derived county) -- never the search box.
        """
        fallback_county = (county or "").lower().strip()
        matches = list(_PARCEL_SPLIT_RE.finditer(text))
        if not matches:
            return []
        parcels: List[PropertyData] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[start:end]
            parcel_no = m.group(1).strip()
            block_county = (GAPublicNoticeScraper._county_from_notice_text(block)
                            or fallback_county).lower().strip()

            addr_m = _GA_PARCEL_ADDR_RE.search(block)
            address = addr_m.group(1).strip() if addr_m else None
            if not address:
                address = f"Parcel {parcel_no}"
            acres = GAPublicNoticeScraper._parse_acres(block)
            desc = block.strip()
            parcel: PropertyData = {
                "source": "ga_publicnotice",
                "source_listing_id": f"{block_county}:{parcel_no}",
                "url": detail_url,
                "address": address[:120] if address else None,
                "city": None,
                "county": block_county,
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

    def scrape(self) -> List[PropertyData]:
        """Run the scraper: search georgiapublicnotice.com and extract qualifying cases."""
        print(f"\n  GA PUBLIC NOTICE FORECLOSURES - {len(COUNTY_SET)} target counties")
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
                print(f"  Keep only notices published in the last {LOOKBACK_DAYS} days")
                all_records = []
                global_seen_pk = set()
                for category in categories:
                    self._select_category(page, category)
                    for county in sorted(COUNTY_SET):
                        old_pks = self._grid_pks(page)
                        self._select_county(page, county)
                        self._submit_search(page)
                        self._wait_grid_refresh(page, old_pks)

                        recs = self._parse_grid_records(page)

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
                                    recs.append(r)
                                # Grid sorts newest-first; stop once the first
                                # row on a page is past the lookback window.
                                if more and not _is_recent_publication(
                                        more[0].get("full_text") or ""):
                                    break
                                nxt = self._page_info(page)
                                if not nxt:
                                    break
                                cur, total = nxt["cur"], nxt["total"]
                        # Drop records published before the lookback window.
                        recs = [r for r in recs
                                if _is_recent_publication(r.get("full_text") or "")]
                        # Collapse notices seen under an earlier county's
                        # (unreliable) checkbox selection; the grid-text county
                        # is authoritative, so the true county governs the
                        # record's *source_listing_id* and dedup.
                        fresh = [r for r in recs
                                 if r["pk_id"] not in global_seen_pk]
                        global_seen_pk.update(r["pk_id"] for r in fresh)
                        all_records.extend(fresh)
                        print(f"    [{category}] {county}: {len(recs)} recent notices, "
                              f"{len(fresh)} new")

                records = all_records
                print("  [3/4] Parsing results ...")
                print(f"  Found {len(records)} total notices")

                target_records = [r for r in records if (r.get("county") or "").lower() in COUNTY_SET]
                print(f"  {len(target_records)} in target counties")

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

    def _select_category(self, page, category: str) -> None:
        """Select a popular-search category (triggers an auto-postback)."""
        page.select_option('select[id*="ddlPopularSearches"]', category)
        page.wait_for_timeout(5000)
        for _ in range(10):
            try:
                page.wait_for_selector('input[id*="lstCounty"]', state="attached", timeout=3000)
                break
            except Exception:
                page.wait_for_timeout(1000)

    def _select_county(self, page, county: str) -> None:
        """Check a single county checkbox (ASP.NET auto-postbacks per check)."""
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

    # ---- detail extraction --------------------------------------------------

    def _extract_detail(self, page, session_id: str, record: dict) -> List[PropertyData]:
        """Navigate to detail, pass the Turnstile gate, extract per-parcel records.

        Returns a LIST of PropertyData (a single GA notice may bundle many
        parcels), or an empty list if the notice is rejected (too short,
        quiet-title / post-sale, non-foreclosure, or no parseable parcels).
        """
        pk_id = record["pk_id"]
        raw_text = self._extract_notice_text(page, session_id, pk_id)
        if not raw_text:
            return []

        # Authoritative classification on the full notice text.
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
        detail_url = f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}"
        parcels = self._parse_parcels(raw_text, county, auction_date, detail_url)
        if not parcels:
            logger.warning("No parcels parsed for %s (county=%s)", pk_id, county)
        return parcels


def scrape_with_enrichment(
    solve_captcha: bool = True,
    enrich: bool = True,
) -> List[PropertyData]:
    """Run the GA tax-foreclosure scraper.

    GA has no statewide parcel/GIS enrichment source, so ``enrich`` is accepted
    for API parity but no map enrichment is performed.
    """
    scraper = GAPublicNoticeScraper(solve_captcha=solve_captcha)
    properties = scraper.run()
    return properties
