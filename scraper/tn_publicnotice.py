"""TN Public Notice foreclosure scraper — tnpublicnotice.com via Camoufox + 2captcha.

tnpublicnotice.com is the "Public Notice" ASP.NET WebForms platform (shared
base in :mod:`scraper.publicnotice_base`). Scrapes foreclosure public notices
for TN mountain counties, keeps only county-trustee *tax* sales, and enriches
acreage/address from TNMap via the street address.
"""
from __future__ import annotations
import datetime
import logging
import re
import sys
from typing import List, Optional

from .base import PropertyData
from .config import (
    config,
    TNFORECLOSURES_BASE_URL,
    TNFORECLOSURES_TURNSTILE_SITE_KEY,
    TNFORECLOSURES_POPULAR_SEARCH_VALUE,
    TN_FORECLOSURE_COUNTIES,
)
from .publicnotice_base import (
    PublicNoticeScraper,
    dedup_by_content,
    extract_street_address,
    PER_PAGE_SELECT,
)

logger = logging.getLogger(__name__)

COUNTY_SET = set(TN_FORECLOSURE_COUNTIES)

# Only process notices published within this many days (the grid is sorted by
# publication date, newest first, so we paginate until we pass the cutoff).
LOOKBACK_DAYS = 7

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


class TNPublicNoticeScraper(PublicNoticeScraper):
    """Scraper for TN public foreclosure notices from tnpublicnotice.com."""

    SOURCE_NAME = "tn_publicnotice"
    BASE_URL = TNFORECLOSURES_BASE_URL
    TURNSTILE_SITE_KEY = TNFORECLOSURES_TURNSTILE_SITE_KEY

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
        """Pull the TN property county from a grid-row's text."""
        if not full_text:
            return None
        for pat in (
            r"TENNESSEE\s*[,:]?\s*([A-Z][A-Za-z]+)\s+COUNTY",
            r"([A-Z][A-Za-z]+)\s+COUNTY\s*[,:]?\s*TENNESSEE",
            r"(?:at\s+the\s+\w+\s+(?:door|entrance|breezeway|steps|lobby)[^,]*,"
            r"\s*)([A-Z][A-Za-z]+)\s+County\s*Courthouse",
            r"([A-Z][A-Za-z]+)\s+County\s*Courthouse",
        ):
            m = re.search(pat, full_text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return None

    @staticmethod
    def _is_publication_notice(text: str) -> bool:
        """Skip court *service* publications that are not parcel sales.

        tnpublicnotice.com mixes in "NOTICE OF PUBLICATION" filings used to
        serve non-resident / cannot-be-located defendants (e.g. consolidated
        delinquent-taxpayer lists naming dozens of parties). These have no
        single street address or auction and must not become "properties".
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

    def scrape(self) -> List[PropertyData]:  # -> List[PropertyData]
        """Run the scraper: search tnpublicnotice.com and extract qualifying cases."""
        print(f"\n  TN PUBLIC NOTICE FORECLOSURES - {len(COUNTY_SET)} target counties")
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
                            stop = True
                            break
                        all_records.append(r)
                    return stop

                stop = _collect(self._parse_grid_records(page))
                print(f"  Page 1: {len(all_records)} kept (last {LOOKBACK_DAYS} days)")

                info = self._page_info(page)
                page_no = 1
                if info:
                    cur, total = info["cur"], info["total"]
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

                # Pre-filter: drop mortgage/deed-of-trust (bank) foreclosures and
                # court *service* publications so we don't burn a Turnstile solve.
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

        properties = dedup_by_content(properties)
        return properties

    # ---- browser interactions ---------------------------------------------

    def _search_foreclosures(self, page) -> None:
        page.select_option(
            'select[name="ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"]',
            TNFORECLOSURES_POPULAR_SEARCH_VALUE,
        )
        page.wait_for_timeout(8000)
        try:
            page.select_option(PER_PAGE_SELECT, "50")
            page.wait_for_timeout(4000)
        except Exception as e:
            logger.warning("Could not raise per-page count: %s", e)

    def _extract_detail(self, page, session_id: str, record: dict) -> Optional[PropertyData]:
        """Navigate to detail page, pass the Turnstile gate, extract notice text."""
        pk_id = record["pk_id"]
        raw_text = self._extract_notice_text(page, session_id, pk_id)
        if not raw_text:
            return None

        # Drop explicit acreage that is below threshold (keep unknown for later GIS).
        acres = self._extract_acreage(raw_text)
        if acres is not None and acres < config.MIN_ACRES:
            return None

        address = extract_street_address(raw_text)

        # Authoritative tax-foreclosure check on the full notice text.
        if not self._is_tax_foreclosure(raw_text):
            logger.info("Dropping non-tax foreclosure %s (mortgage/bank)", pk_id)
            return None

        # Reject court *service* publications (non-resident / cannot-be-located
        # delinquent-taxpayer lists) even though they mention "delinquent tax" —
        # they name dozens of parties with no single parcel or auction.
        if self._is_publication_notice(raw_text):
            logger.info("Dropping court publication %s (non-resident service list)", pk_id)
            return None

        detail_url = f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}"
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


def scrape_with_enrichment(
    solve_captcha: bool = True,
    enrich: bool = True,
) -> List[PropertyData]:
    """Run TN foreclosure scraper with optional TNMap enrichment."""
    from .tnmap import enrich_with_tnmap

    scraper = TNPublicNoticeScraper(solve_captcha=solve_captcha)
    properties = scraper.run()

    if enrich and properties:
        try:
            properties = enrich_with_tnmap(properties)
        except Exception as e:
            logger.warning("TNMap enrichment failed (keeping scraped props): %s", e)

    return properties
