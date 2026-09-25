"""NC Public Notice foreclosure scraper — ncnotices.com via Camoufox.

Turnstile challenges are solved in-browser by the camoufox page itself
(no third-party solving service).


ncnotices.com is the same "Public Notice" ASP.NET WebForms platform as
tnpublicnotice.com / georgiapublicnotice.com (shared base in
:mod:`scraper.publicnotice_base`). The public "Foreclosure" popular search
needs no login: we page the GridView (newest first, 7-day lookback), keep
rows in the 20 NC mountain counties, and for each result solve the
Cloudflare Turnstile gate and download the notice PDF (the on-page HTML is
a truncated OCR conversion; the PDF is canonical). Missing acreage is
enriched from NC OneMap via the parcel number.
"""
from __future__ import annotations
import logging
import random
import re
import time
from typing import List, Optional

from .base import PropertyData
from .config import (
    config,
    NCFORECLOSURES_BASE_URL,
    NC_MOUNTAIN_COUNTIES,
    NCNOTICES_TURNSTILE_SITE_KEY,
)
from .publicnotice_base import (
    LOOKBACK_DAYS,
    PER_PAGE_SELECT,
    PublicNoticeScraper,
    _is_recent_publication,
)
from .rawlog import log_raw

logger = logging.getLogger(__name__)

COUNTY_SET = set(NC_MOUNTAIN_COUNTIES)

# Public popular-search label for foreclosure notices (same platform as TN).
POPULAR_SEARCH = "Foreclosure"

COUNTY_LABEL_ATTR = "ctl00_ContentPlaceHolder1_as1_lstCounty_"

# --- notice-text parsing (NC-specific) --------------------------------------
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December"
)
_DATE_CORE = r"(?:" + _MONTHS + r")\s+\d{1,2},\s*\d{4}"
_SALE_DATE_RE = re.compile(
    r"\bon(?=[:\s])[:\s]*\s*\n?\s*(" + _DATE_CORE +
    r"(?:\s+AT\s+\d{1,2}:\d{2}(?:\s*[AP]\.?M\.?)?)?)",
    re.IGNORECASE,
)
_CASE_RE = re.compile(r"\b(\d{2}\s?(?:SP|CV|GS|ME)\d{5,9}(?:-\d+)?)\b")
_PIN_RE = re.compile(
    r"(?:parcel(?:\s*(?:id|no\.?|#))?|tax\s*(?:id|parcel)|p\.?i\.?n\.?)"
    r"\s*[:#]?\s*([0-9][0-9A-Za-z\-]{3,19})",
    re.IGNORECASE,
)
_COUNTY_OF_RE = re.compile(r"\bCOUNTY\s+OF\s+([A-Z][A-Za-z]+)\b")
_COUNTY_NAME_RE = re.compile(r"\b([A-Z][A-Za-z]+)\s+COUNTY\b")
_NC_HEADER_RE = re.compile(
    r"NORTH\s+CAROLINA[,\s:]+([A-Z][A-Za-z]+)\s+COUNTY\b", re.IGNORECASE
)
_ADDR_RE = re.compile(
    r"(?:street\s+)?address\s*:\s*(.{5,80}?(?:NC|,\s*\d{5}|\d{5}))", re.IGNORECASE
)
_COUNTY_STOPWORDS = {
    "registry", "register", "clerk", "superior", "court", "deed",
    "judicial", "superiorcourt", "chancery", "orphan",
}

# ncnotices.com's saved search returns a broad mix. We keep ONLY genuine *tax*
# foreclosures (positive tax-sale language); mortgage/deed-of-trust and
# HOA/assessment liens are excluded.
_MORTGAGE_RE = re.compile(
    r"deed\s+of\s+trust|deed\s+oftrust|substitute\s+trustee|"
    r"trustee'?s?\s+sale|power\s+of\s+sale|foreclosure\s+of\s+a\s+deed|"
    r"notice\s+of\s+trustee'?s?\s+sale",
    re.IGNORECASE,
)
_TAX_RE = re.compile(
    r"foreclosure\s+sale\s+to\s+satisfy\s+unpaid|"
    r"satisfy\s+unpaid\s+(?:property\s+)?taxes|"
    r"unpaid\s+property\s+taxes\s+owing|"
    r"taxes\s+owing\s+to|"
    r"\btax\s+foreclosure\b|"
    r"foreclosure\s+(?:of|for)\s+(?:the\s+)?tax|"
    r"tax\s+lien|lien\s+(?:for|of)\s+tax|"
    r"in\s+rem\s+foreclosure|"
    r"delinquent\s+tax|"
    r"commissioner\s+of\s+(?:revenue|taxes)|"
    r"chapter\s+105|"
    r"tax\s+sale|"
    r"certificate\s+of\s+tax|"
    r"delinquent\s+ad\s+valorem",
    re.IGNORECASE,
)


def _row_county(text: str) -> Optional[str]:
    """Extract the publication-county token from a results-grid row."""
    m = re.search(r"county:\s*([A-Za-z]+)", text, re.IGNORECASE)
    return m.group(1).lower() if m else None


def _find_auction_date(text: str) -> Optional[str]:
    """Find the sale/auction date in notice text."""
    m = _SALE_DATE_RE.search(text)
    return m.group(1).strip() if m else None


def _extract_county(text: str, all_counties: set[str]) -> Optional[str]:
    """Return the property county (lowercase) named in the notice header."""
    head = text[:1200]
    for regex in (_NC_HEADER_RE, _COUNTY_OF_RE):
        for m in regex.finditer(head):
            name = m.group(1).strip().lower()
            if name in all_counties:
                return name
    for m in _COUNTY_NAME_RE.finditer(head):
        name = m.group(1).strip().lower()
        if name in _COUNTY_STOPWORDS:
            continue
        if name in all_counties:
            return name
    return None


def _extract_address(text: str, county: str = "") -> Optional[str]:
    from .courthouses import is_courthouse_address
    for m in _ADDR_RE.finditer(text or ""):
        addr = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
        if addr and not is_courthouse_address(addr, county, "NC"):
            return addr
    return None


def _extract_pin(text: str) -> Optional[str]:
    for m in _PIN_RE.finditer(text):
        return m.group(1)
    return None


def _is_tax_foreclosure(notice: str) -> bool:
    """Return True only for genuine tax-foreclosure sale notices (NC)."""
    if not _TAX_RE.search(notice or ""):
        return False
    if _MORTGAGE_RE.search(notice or ""):
        return False
    return True


def _classify_nc_notice(notice: str) -> Optional[str]:
    """Classify an NC notice as tax sale, mortgage sale, or neither (drop).

    Tax requires positive tax-sale language with no mortgage signal;
    mortgage-only notices (deed-of-trust / substitute-trustee sales) are kept
    for the Mtg tab; HOA/assessment/other liens (neither signal) are dropped.
    """
    text = notice or ""
    has_tax = _TAX_RE.search(text) is not None
    has_mortgage = _MORTGAGE_RE.search(text) is not None
    if has_tax and not has_mortgage:
        return "tax_foreclosure"
    if has_mortgage and not has_tax:
        return "mortgage_foreclosure"
    return None


class NCPublicNoticeScraper(PublicNoticeScraper):
    """Scrape ncnotices.com tax/foreclosure notices for the 20 NC mountain counties."""

    SOURCE_NAME = "nc_publicnotice"
    BASE_URL = NCFORECLOSURES_BASE_URL
    TURNSTILE_SITE_KEY = NCNOTICES_TURNSTILE_SITE_KEY

    def __init__(self, max_candidates: int = 600):
        super().__init__(search_type="foreclosure", delay=1.5,
                         use_proxy=False, solve_captcha=True)
        self.max_candidates = max_candidates
        self._all_counties: set[str] = set()

    def _get_target_counties(self) -> set[str]:
        return COUNTY_SET

    def _county_from_grid_text(self, full_text: str) -> Optional[str]:
        """Pull the publication county from a grid row (``County: <Name>``)."""
        return _row_county(full_text)

    # ------------------------------------------------------------------
    # browser interactions (public popular search — no login)
    # ------------------------------------------------------------------

    def _search_foreclosures(self, page) -> None:
        """Select the public Foreclosure popular search and widen the grid."""
        page.select_option(
            'select[name="ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"]',
            POPULAR_SEARCH,
        )
        page.wait_for_timeout(8000)
        # Capture the site's full county list for notice-county validation.
        self._all_counties = set(page.evaluate(
            "prefix => Array.from(document.querySelectorAll("
            "'label[for^=\"' + prefix + '\"]'))"
            "  .map(l => (l.textContent || '').trim().toLowerCase())"
            "  .filter(Boolean)",
            COUNTY_LABEL_ATTR))
        if not self._all_counties:
            logger.warning("could not read county list from search form")
        try:
            page.select_option(PER_PAGE_SELECT, "50")
            page.wait_for_timeout(4000)
        except Exception as e:
            logger.warning("Could not raise per-page count: %s", e)

    def _build_property(self, cand: dict, notice: str, url: str) -> Optional[PropertyData]:
        county = _extract_county(notice, self._all_counties)
        if county not in COUNTY_SET:
            log_raw(
                self.SOURCE_NAME, listing_id=cand.get("id"),
                county=county, state="NC",
                decision="dropped_county",
                reason="property county outside 20-county target set",
                raw_text=notice, url=url,
            )
            return None

        kind = _classify_nc_notice(notice)
        if kind is None:
            log_raw(
                self.SOURCE_NAME, listing_id=cand.get("id"),
                county=county, state="NC",
                decision="dropped_non_foreclosure",
                reason="no tax-only or mortgage-only signal (HOA/other lien?)",
                raw_text=notice, url=url,
            )
            logger.info("Skipping non-foreclosure (HOA/other lien) for %s",
                        cand.get("id"))
            return None

        acres = self._extract_acreage(notice)
        case_m = _CASE_RE.search(notice)
        auction_date = _find_auction_date(notice)
        pin = _extract_pin(notice)
        address = _extract_address(notice, county or "")

        # Map links at build time (pure URL builders, no network) so the
        # Telegram alert sent at insert time already carries Maps + GIS.
        # build_gis_url returns None unless a parcel/coordinates back it
        # (parcel-deep-or-nothing); Maps needs a real street address.
        from .nc_gis_lookup import build_gis_url, build_google_maps_url
        gis_url = build_gis_url(None, None, pin, address, county, state="NC")
        google_maps_url = (
            build_google_maps_url(None, None, address, None, county, state="NC")
            if address else None
        )

        log_raw(
            self.SOURCE_NAME, listing_id=cand.get("id"),
            county=county, state="NC",
            decision="kept_tax" if kind == "tax_foreclosure" else "kept_mortgage",
            reason=f"public search; pin={pin}",
            raw_text=notice, url=url,
        )
        return PropertyData(
            source=self.SOURCE_NAME,
            source_listing_id=cand["id"],
            url=url,
            address=address,
            city=None,
            county=county,
            state="NC",
            zip_code=None,
            latitude=None,
            longitude=None,
            price=1,
            acres=acres,
            description=notice[:2000],
            property_type=kind,
            court_case=case_m.group(1).replace(" ", "") if case_m else None,
            auction_date=auction_date,
            parcel_number=pin,
            raw_source_text=notice,
            gis_url=gis_url,
            google_maps_url=google_maps_url,
        )

    # ------------------------------------------------------------------
    # GIS acreage enrichment (in-memory, before acreage filter)
    # ------------------------------------------------------------------

    def _enrich_acres(self, props: list[PropertyData]) -> list[PropertyData]:
        """Fill missing acreage (and coords/address) from NC OneMap via PIN."""
        from .nc_gis_lookup import NC1MapService
        svc = NC1MapService()
        enriched = 0
        for p in props:
            if p.get("acres") is not None or not p.get("parcel_number"):
                continue
            try:
                data = svc.by_parcel(p["parcel_number"], county=p.get("county"))
            except Exception as e:
                logger.warning("GIS lookup failed for %s: %s", p["parcel_number"], e)
                data = None
            if data and data.get("acres"):
                p["acres"] = data["acres"]
                p["acres_source"] = "gis"
                if data.get("latitude"):
                    p["latitude"] = data["latitude"]
                if data.get("longitude"):
                    p["longitude"] = data["longitude"]
                if not p.get("address") and data.get("siteadd"):
                    p["address"] = data["siteadd"]
                if not p.get("parcel_number") and data.get("parno"):
                    p["parcel_number"] = data["parno"]
                # Backfill map links when enrichment supplied coords/address
                # after build time (keeps Telegram-at-insert-time complete).
                if (p.get("latitude") is not None
                        and p.get("longitude") is not None):
                    from .nc_gis_lookup import build_gis_url, build_google_maps_url
                    p["gis_url"] = p.get("gis_url") or build_gis_url(
                        p["longitude"], p["latitude"], p.get("parcel_number"),
                        p.get("address"), p.get("county"), state="NC")
                    p["google_maps_url"] = p.get("google_maps_url") or build_google_maps_url(
                        p["longitude"], p["latitude"], p.get("address"), None,
                        p.get("county"), state="NC")
                enriched += 1
            time.sleep(0.6)
        print(f"  GIS acreage enrichment: {enriched} of {len(props)} filled")
        return props

    # ------------------------------------------------------------------
    # main flow
    # ------------------------------------------------------------------

    @staticmethod
    def _notice_id(record: dict) -> Optional[str]:
        """Numeric notice ID, preserving the pre-refactor listing-id scheme.

        The GridView detail buttons link ``Details.aspx?...&ID=<n>`` — the
        same ID space the saved-search grid used — so reuse it to keep
        ``source_listing_id`` stable across the login-free migration.
        """
        m = re.search(r"[?&]ID=(\d+)", record.get("detail_url") or "")
        if m:
            return m.group(1)
        return record.get("sp_case") or record.get("pk_id")

    def scrape(self) -> List[PropertyData]:
        """Run the scraper: public Foreclosure search, no login required."""
        print(f"\n  NC PUBLIC NOTICE FORECLOSURES (ncnotices.com public search)")
        print(f"  Search: '{POPULAR_SEARCH}' popular search, no login")
        print(f"  Target counties: {len(COUNTY_SET)}")
        print(f"  Proxy: {'enabled' if self.use_proxy else 'disabled'}")
        print(f"  Captcha solving: {'enabled' if self.solve_captcha else 'disabled (search only)'}")
        print()

        properties: List[PropertyData] = []

        proxy_cfg = {"server": config.PROXY_URL} if self.use_proxy and config.PROXY_URL else None

        from .base import camoufox_context
        with camoufox_context(proxy=proxy_cfg) as page:
            page.set_viewport_size({"width": 1600, "height": 1000})
            page.set_default_timeout(45000)

            try:
                print("  [1/4] Connecting to ncnotices.com ...", end=" ", flush=True)
                page.goto(self.BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
                session_id = self._extract_session(page.url)
                if not session_id:
                    logger.error("Could not extract session ID")
                    return []
                print(f"session={session_id}")

                print("  [2/4] Searching foreclosure notices ...", end=" ", flush=True)
                self._search_foreclosures(page)
                print("done")

                # Loud by design: a missing grid means the site layout
                # changed (outage), not a quiet week — fail instead of
                # recording "completed, 0 found".
                has_grid = page.evaluate(
                    "() => !!document.querySelector('table[id*=\"GridView\"]')")
                if not has_grid:
                    raise RuntimeError(
                        "search results grid did not appear after "
                        "popular-search selection")

                print("  [3/4] Parsing results, one county search at a time ...")
                print(f"  Keep only notices published in the last {LOOKBACK_DAYS} days")

                candidates: List[dict] = []
                seen: set[str] = set()

                def _collect(recs):
                    stop = False
                    for r in recs:
                        nid = self._notice_id(r)
                        if not nid or nid in seen:
                            continue
                        row_text = r.get("full_text") or ""
                        # The grid-text county stays authoritative: the
                        # checkbox filter is unreliable and other counties'
                        # rows can leak into a county search.
                        rc = (r.get("county") or "").lower() or None
                        if rc and rc not in COUNTY_SET:
                            continue
                        if not _is_recent_publication(row_text):
                            continue
                        seen.add(nid)
                        candidates.append({"id": nid, "pk_id": r.get("pk_id"),
                                           "county": r.get("county")})
                    # Grid sorts newest-first; stop once even the freshest
                    # (first) row on a page is past the lookback window.
                    if recs and not _is_recent_publication(
                            recs[0].get("full_text") or ""):
                        stop = True
                    return stop

                def _walk_county_pages(county: str) -> None:
                    """Page through the current county grid until the
                    lookback stop, the candidate cap, or 50 pages."""
                    stop = _collect(self._parse_grid_records(page))
                    print(f"    [{county}] page 1: {len(candidates)} kept "
                          f"(total {len(candidates)})")
                    info = self._page_info(page)
                    page_no = 1
                    if not info:
                        return
                    cur, total = info["cur"], info["total"]
                    while (cur < total and not stop
                           and len(candidates) < self.max_candidates
                           and page_no < 50):
                        if not self._goto_next_page(page, cur + 1):
                            break
                        page_no += 1
                        before = len(candidates)
                        stop = _collect(self._parse_grid_records(page))
                        print(f"    [{county}] page {page_no}: "
                              f"+{len(candidates) - before} kept "
                              f"(total {len(candidates)})")
                        nxt = self._page_info(page)
                        if not nxt:
                            break
                        cur, total = nxt["cur"], nxt["total"]

                for county in sorted(COUNTY_SET):
                    if len(candidates) >= self.max_candidates:
                        print(f"  Candidate cap ({self.max_candidates}) "
                              "reached; stopping county sweep")
                        break
                    if not self._set_county_filter(page, county):
                        logger.error("skipping %s: county filter unavailable",
                                     county)
                        continue
                    self._submit_county_search(page)
                    self._widen_grid(page)
                    _walk_county_pages(county)

                candidates = candidates[:self.max_candidates]
                print(f"  Found {len(candidates)} notices in last {LOOKBACK_DAYS} days")

                print(f"  [4/4] Extracting details ({len(candidates)} cases) ...")
                for i, cand in enumerate(candidates, 1):
                    print(f"    [{i}/{len(candidates)}] {cand['id']} - {cand.get('county', '?')}",
                          end=" ", flush=True)
                    time.sleep(random.uniform(1.0, 2.0))
                    detail_url = (f"{self.BASE_URL}/(S({session_id}))/Details.aspx"
                                  f"?SID={session_id}&ID={cand['id']}")
                    try:
                        notice = self._extract_notice_text(
                            page, session_id, cand["id"])
                    except Exception as e:
                        logger.warning("detail error for %s: %s", cand["id"], e)
                        notice = None
                    if not notice:
                        print("(skipped)")
                        continue
                    prop = self._build_property(cand, notice, detail_url)
                    if prop:
                        print(f"-> {prop['county']} "
                              f"{prop.get('acres') if prop.get('acres') is not None else '?'}ac")
                        properties.append(prop)
                    else:
                        print("(non-target county)")

            finally:
                pass

        return properties

    def run(self) -> list[PropertyData]:
        """Scrape, GIS-enrich acreage, then filter by county + MIN_ACRES.

        Loud by design: failures propagate to run_scraper (status='failed'
        + error_message) instead of degrading to "completed, 0 found".
        """
        props = self.scrape()
        print(f"  Scrape: {len(props)} target-county properties")

        props = self._enrich_acres(props)
        return self._apply_county_acreage_filter(props)


def scrape_all() -> list[PropertyData]:
    return NCPublicNoticeScraper().run()
