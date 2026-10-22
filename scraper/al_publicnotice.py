"""AL Public Notice foreclosure scraper — alabamapublicnotices.com (APA).

alabamapublicnotices.com is the same "Public Notice" ASP.NET WebForms
platform (shared base in :mod:`scraper.publicnotice_base`) as
ncnotices.com / tnpublicnotice.com / georgiapublicnotice.com — verified live
2026-10-22 (session-in-URL Search.aspx, hdnPKValue GridView, Turnstile detail
gate whose sitekey is identical to GA's). Scrapes the "Foreclosures" and
"Delinquent Taxes" popular-search categories for the 5 AL mountain counties;
county sales go to the Tax tab and mortgage power-of-sale foreclosures to the
Mtg tab.

AL-specific notes:
* AL mortgage foreclosures advertise as "MORTGAGE FORECLOSURE SALE" /
  "NOTICE OF SALE UNDER POWER" ("under and by virtue of the power of sale
  contained in said mortgage") — the on-page OCR text is frequently
  hyphen-split ("MORT-GAGE FORECLOS-URE SALE"), so the classifier matches
  hyphen-stripped copies too.
* AL tax notices cite the tax collecting official and Code of Alabama
  Title 40 (delinquent-tax notices / tax-lien auctions under §40-10).
* Alabama publishes no statewide parcel service per the GIS link policy
  (like SC): rows get Google Maps links only, no GIS viewer.
"""
from __future__ import annotations
import logging
import re
import sys
from typing import List, Optional

from .base import PropertyData
from .config import (
    config,
    ALFORECLOSURES_BASE_URL,
    ALFORECLOSURES_TURNSTILE_SITE_KEY,
    ALFORECLOSURES_CATEGORIES,
    AL_FORECLOSURE_COUNTIES,
)
from .publicnotice_base import (
    LOOKBACK_DAYS,
    MORTGAGE_FC_PATTERNS,
    PublicNoticeScraper,
    STREET_TAIL_BODY,
    HWYN_TAIL_BODY,
    _is_recent_publication,
    normalize_notice_text,
)
from .rawlog import log_raw

logger = logging.getLogger(__name__)

COUNTY_SET = set(AL_FORECLOSURE_COUNTIES)

# "County: <Name>" publication-county marker in AL grid rows (the row head is
# "<Paper> <Weekday, Month D, YYYY> County: <Name> <NOTICE TEXT> ...").
_ROW_COUNTY_RE = re.compile(r"county:\s*([A-Za-z]+)", re.IGNORECASE)

# Alabama mortgage/bank foreclosures. AL uses "power of sale" under a
# mortgage (no deed of trust); on-page OCR hyphen-splits words
# ("MORT-GAGE FORECLOS-URE SALE", "GLEN N GREES"). The hyphen-stripped
# classification copy (see _al_classify) resolves these.
AL_MORTGAGE_FC_PATTERNS = [
    r"mort\s*gage\s*fore\s*clos\s*ure",
    r"sale\s*under\s*power",
    r"power\s*of\s*sale",
    r"notice\s*of\s*fore\s*clos\s*ure",
    r"substitute\s*trustee",
    r"security\s*deed",
]
# Signals that a notice is a genuine AL tax foreclosure: the tax
# collecting official (judge of probate / revenue commissioner) selling
# real estate for delinquent taxes, citing Code of Alabama Title 40.
AL_TAX_FC_PATTERNS = [
    r"tax\s*collecting\s*official",
    r"delinquent\s*tax",
    r"tax\s*sale",
    r"tax\s*lien",
    r"section\s*40-10",
    r"delinquent\s*real\s*estate",
    r"tax\s*redemptions?",
]

# Cheaper-than-a-Turnstile-solve grid-level skips: personal-property sales
# (abandoned vehicles / storage liens), probate & name-change & juvenile
# court filings — neither mortgage nor tax foreclosures of real property.
_GRID_SKIP_PATTERNS = [
    r"abandoned\s*vehicle",
    r"abandoned\s*vessel",
    r"storage\s*funded|self\s*storage",
    r"probate\s*court|letters\s*of\s*administration|notice\s*to\s*creditors",
    r"juvenile\s*court|petition\s*to\s*change\s*name",
    r"marriage\s*license|estranged|cast\s*iron",  # classified-ad filler
    # city/county bid solicitations, court service actions that aren't
    # property sales (live Talladega rows: bid ad, DOMESTIC RELATIONS)
    r"advertisement\s*for\s*bid|invitation\s*to\s*bid|sealed\s*proposal|"
    r"request\s*for\s*qualification",
    r"domestic\s*relations|dissolution\s*of\s*marriage|protection\s*"
    r"order\s*petition",
    r"notice\s*of\s*taking|articles\s*of\s*incorporation|annual\s*report",
]

_AL_PARCEL_RE = re.compile(
    r"\b\d{1,2}-\d{1,2}-\d{1,2}-\d{1,2}-\d{1,3}-\d{1,3}(?:\.\d{3})?\b")
_AL_LABELED_PARCEL_RE = re.compile(
    r"(?:parcel|tax\s*parcel|tax\s*id)\s*(?:\(s\))?\s*(?:id(?:entification)?\s*)?"
    r"(?:number|num\.?|no\.?)?(?:\s+(?:is|=|and))?\s*[:#]?\s*"
    r"([0-9][0-9A-Za-z\-\.\/,\s]{4,39})",
    re.IGNORECASE)

_AL_SALE_ANCHORS = (
    "will sell", "sell at public outcry", "public outcry",
    "auction", "sale date", "date of sale", "offered for sale",
    "drawn for sale", "will offer",
)
_AL_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_AL_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2}(?:st|nd|rd|th)?),?\s+(\d{4})",
    re.IGNORECASE)
# Deed-recital anchors: a date here is the loan/deed date, not the auction.
_AL_DEED_ANCHOR_RE = re.compile(
    r"dated|record(ed)?|executed|delivered|conveyed|recording|instrument",
    re.IGNORECASE)


def _dehyphenate(text: str) -> str:
    """Remove hyphens so OCR hyphen-splits classify ("MORT-GAGE" → "MORTGAGE")."""
    return (text or "").replace("-", "").lower()


def _matches_any(text: str, patterns) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


class ALPublicNoticeScraper(PublicNoticeScraper):
    """Scraper for AL tax/mortgage foreclosure notices on alabamapublicnotices.com."""

    SOURCE_NAME = "al_publicnotice"
    BASE_URL = ALFORECLOSURES_BASE_URL
    TURNSTILE_SITE_KEY = ALFORECLOSURES_TURNSTILE_SITE_KEY

    def __init__(
        self,
        search_type: str = "foreclosure",
        delay: float = 1.5,
        use_proxy: bool = False,   # shared APA/IPA infra; same rationale as GA
        solve_captcha: bool = True,
        lookback_days: int = LOOKBACK_DAYS,
    ):
        super().__init__(search_type=search_type, delay=delay,
                         use_proxy=use_proxy, solve_captcha=solve_captcha,
                         lookback_days=lookback_days)

    def _get_target_counties(self) -> set[str]:
        return COUNTY_SET

    def _county_from_grid_text(self, full_text: str) -> Optional[str]:
        """Pull the AL publication county from a grid row head."""
        if not full_text:
            return None
        m = _ROW_COUNTY_RE.search(full_text[:250])
        return m.group(1).lower() if m else None

    # ---- classification --------------------------------------------------

    @classmethod
    def _classify(cls, text: str) -> Optional[str]:
        """Tax vs mortgage vs drop on the full notice text (hyphen-hardened).

        Run against both the (space-normalized) original and a
        hyphen-stripped copy, and against the shared NC/TN/GA pattern sets
        plus the AL-specific ones.
        """
        raw = normalize_notice_text(text or "")
        flat = _dehyphenate(raw)
        has_mtg = (
            _matches_any(flat, AL_MORTGAGE_FC_PATTERNS)
            or any(re.search(p, raw.lower()) for p in MORTGAGE_FC_PATTERNS)
        )
        # Tax signals must come from the AL-specific statutory set — the
        # shared TN/GA tax patterns reference foreign statutes and the bare
        # "Code of Alabama" phrasing lives in the site footer of every
        # detail page (live Talladega run misclassified everything).
        # Match on BOTH copies: the hyphen-stripper fuses "40-10" to
        # "4010180", so statutory cites only exist on the raw copy.
        has_tax = (
            _matches_any(flat, AL_TAX_FC_PATTERNS)
            or any(re.search(p, raw.lower()) for p in AL_TAX_FC_PATTERNS)
        )
        if has_tax:
            return "tax_foreclosure"
        if has_mtg:
            return "mortgage_foreclosure"
        return None

    @classmethod
    def _grid_skip(cls, text: str) -> bool:
        """Cheap grid-level skip for non-property notices."""
        low = (text or "").lower()
        if "abandoned" in low:
            return True
        flat = _dehyphenate(low)
        return _matches_any(flat, _GRID_SKIP_PATTERNS)

    # ---- detail parsing ---------------------------------------------------

    @staticmethod
    def _extract_address(text: str, county: str = "") -> Optional[str]:
        """Property address from an AL sale notice.

        Prefers labeled property-address / known-as clauses, falls back to the
        shared generic extractor. County-courthouse / sale-venue addresses
        ("before the courthouse door") are rejected via the courthouse
        registry.
        """
        from .courthouses import is_courthouse_address
        if not text:
            return None
        body = normalize_notice_text(text)
        labeled = [
            r"(?:property\s+(?:address|location)|street\s+address|located\s+at"
            r"|(?:(?:commonly|also)\s+)?known\s+as)"
            r"\s*[:\-]?\s*(\d{1,5}\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+){0,4}\s*"
            rf"{STREET_TAIL_BODY}"
            rf"|\d{{1,5}}\s+(?:OLD\s+)?{HWYN_TAIL_BODY})",
        ]
        for pat in labeled:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                addr = re.sub(r"\s+", " ", m.group(1).strip())
                if addr and not is_courthouse_address(addr, county, "AL"):
                    return addr[:120]
        # No labeled clause (metes-and-bounds-only notices) -> no address.
        # The generic street regex matches INSIDE legal descriptions
        # ("1 and 2 of Blackstone Estates", "9 East", township ranges) —
        # live row #844 produced that junk; a NULL address is safer than a
        # fabricated one because the Maps/GIS buttons hang on it.
        return None

    @classmethod
    def _extract_sale_date(cls, text: str) -> Optional[str]:
        """Auction date: prefer dates anchored by sale intent, reject deed dates.

        AL sale notices anchor the auction with "will sell ... on <date>",
        "sell at public outcry ... the <date>"; deed/loan recitals carry
        older "dated/recorded/executed" dates. Candidates inside a sale-
        anchored window win; ties go to the later date.
        """
        if not text:
            return None
        flat = normalize_notice_text(text)
        best = None  # (anchored, iso)
        for m in _AL_DATE_RE.finditer(flat):
            mon = _AL_MONTHS.get(m.group(1).title())
            day = int(re.sub(r"(st|nd|rd|th)$", "", m.group(2), flags=re.I))
            if not mon:
                continue
            try:
                from datetime import date as _date
                from datetime import timedelta as _td
                iso = _date(int(m.group(3)), mon, day).isoformat()
            except (TypeError, ValueError):
                continue
            before = flat[max(0, m.start() - 70):m.start()].lower()
            anchored = any(a in before for a in _AL_SALE_ANCHORS)
            if not anchored and _AL_DEED_ANCHOR_RE.search(flat[max(0, m.start() - 45):m.start()]):
                continue
            if best is None or (anchored, iso) >= best:
                best = (anchored, iso)
        return best[1] if best else None

    @classmethod
    def _extract_parcel(cls, text: str) -> Optional[str]:
        """AL assessor parcel / tax-ID number from notice text."""
        if not text:
            return None
        m = _AL_PARCEL_RE.search(text)
        if m:
            return m.group(0)
        flat = normalize_notice_text(text)
        m = _AL_LABELED_PARCEL_RE.search(flat)
        if m:
            pin = re.sub(r"[,\s]+", "", m.group(1).rstrip(" .,;"))[:40]
            return pin or None
        return None

    # ---- flow -------------------------------------------------------------

    def scrape(self) -> List[PropertyData]:
        print(f"\n  AL PUBLIC NOTICE FORECLOSURES - {len(COUNTY_SET)} target counties")
        print(f"  Categories: {self._categories()}")
        print(f"  Proxy: {'enabled' if self.use_proxy else 'disabled'}")
        print(f"  Captcha solving: {'enabled' if self.solve_captcha else 'disabled (search only)'}")

        properties: List[PropertyData] = []
        proxy_cfg = {"server": config.PROXY_URL} if self.use_proxy and config.PROXY_URL else None

        from .base import camoufox_context
        with camoufox_context(proxy=proxy_cfg) as page:
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            try:
                print("  [1/4] Connecting to alabamapublicnotices.com ...",
                      end=" ", flush=True)
                page.goto(self.BASE_URL + "/", wait_until="domcontentloaded",
                          timeout=30000)
                session_id = self._extract_session(page.url)
                if not session_id:
                    logger.error("Could not extract AL session ID")
                    return []
                print(f"session={session_id}")

                print(f"  [2/4] Category + per-county searches (lookback "
                      f"{self._window_days()}d) ...")
                all_records = []
                global_seen_pk = set()
                for category in self._categories():
                    # A fresh page load per category: the ASP.NET
                    # ddlPopularSearches pivot needs a clean postback
                    # context — live verified that without it the SECOND
                    # category's county searches all came back empty
                    # ("results grid missing after county submit").
                    page.goto(self.BASE_URL + "/",
                              wait_until="domcontentloaded", timeout=30000)
                    session_id = self._extract_session(page.url) or session_id
                    if not self._select_category(page, category):
                        logger.error("AL category %r unavailable", category)
                        continue
                    for county in sorted(COUNTY_SET):
                        if not self._set_county_filter(page, county):
                            logger.error("skipping %s: AL county filter unavailable",
                                         county)
                            continue
                        self._submit_county_search(page)
                        self._widen_grid(page)
                        county_recs = self._walk_county_pages(page, county)
                        fresh = [r for r in county_recs
                                 if r["pk_id"] not in global_seen_pk]
                        global_seen_pk.update(r["pk_id"] for r in fresh)
                        all_records.extend(fresh)
                        print(f"    [{category}] {county}: {len(county_recs)} "
                              f"notices, {len(fresh)} new")

                records = all_records
                # Detail URLs need a CURRENT session id — the per-category
                # navigations may have rotated it.
                session_id = self._extract_session(page.url) or session_id
                print(f"  Found {len(records)} notices in last {self._window_days()} days")
                target = [r for r in records
                          if (r.get("county") or "") in COUNTY_SET]
                print(f"  {len(target)} in target counties")
                pre = []
                grid_drop_skipped = 0
                for r in target:
                    if self._grid_skip(r.get("full_text") or ""):
                        grid_drop_skipped += 1
                    else:
                        pre.append(r)
                print(f"  {grid_drop_skipped} non-property notices skipped at grid; "
                      f"{len(pre)} detail candidates remain")

                print(f"  [3/4] Extracting details ({len(pre)} notices) ...")
                for i, rec in enumerate(pre):
                    print(f"    [{i+1}/{len(pre)}] {rec['pk_id']} - {rec.get('county', '?')}",
                          end=" ", flush=True)
                    try:
                        prop = self._extract_detail(page, session_id, rec)
                    except Exception as e:
                        logger.warning("AL detail extraction failed for %s: %s",
                                       rec.get("pk_id"), e)
                        prop = None
                    if prop:
                        print("-> kept")
                        properties.append(prop)
                    else:
                        print("(skipped)")
            finally:
                pass

        return properties

    def _categories(self) -> List[str]:
        return [c.strip() for c in ALFORECLOSURES_CATEGORIES.split(",") if c.strip()]

    def _select_category(self, page, category: str) -> bool:
        """Pick an AL popular-search category (option matches by LABEL)."""
        try:
            page.select_option('select[id*="ddlPopularSearches"]', label=category)
        except Exception as e:
            logger.debug("AL category select by label failed: %s", str(e)[:200])
            try:
                page.select_option('select[id*="ddlPopularSearches"]', category)
            except Exception as e2:
                logger.error("AL category %r not selectable: %s", category,
                             str(e2)[:200])
                return False
        page.wait_for_timeout(5000)
        for _ in range(10):
            try:
                page.wait_for_selector('input[id*="lstCounty"]',
                                       state="attached", timeout=3000)
                return True
            except Exception:
                page.wait_for_timeout(1000)
        return False

    def _walk_county_pages(self, page, county: str) -> List[dict]:
        """Collect the county grid pages within the publication window."""
        recs = self._parse_grid_records(page)
        if not recs and self._page_info(page) is None:
            # The site rendered "No public notices found" — no pager, no rows.
            return []
        seen_pk = set(r["pk_id"] for r in recs)
        info = self._page_info(page)
        cur, total = (info["cur"], info["total"]) if info else (1, 1)
        while cur < total:
            if not self._goto_next_page(page, cur + 1):
                break
            more = self._parse_grid_records(page)
            if not more:
                break
            for r in more:
                if r["pk_id"] not in seen_pk:
                    seen_pk.add(r["pk_id"])
                    recs.append(r)
            # grids sort newest-first: stop when the page's first row is old
            if not _is_recent_publication(more[0].get("full_text") or "",
                                          days=self._window_days()):
                break
            nxt = self._page_info(page)
            if not nxt:
                break
            cur, total = nxt["cur"], nxt["total"]
        recs = [r for r in recs
                if _is_recent_publication(r.get("full_text") or "",
                                          days=self._window_days())]
        # Attribute rows the grid-text county parser missed to the searched
        # county (the server-side filter was active for this search).
        for r in recs:
            if not r.get("county"):
                r["county"] = county
        return recs

    def _extract_detail(self, page, session_id: str, record: dict) -> Optional[PropertyData]:
        """Detail page -> classify -> build one Property-data row."""
        pk_id = record["pk_id"]
        county = (record.get("county") or "").lower().strip()
        raw_text = self._extract_notice_text(page, session_id, pk_id)
        if not raw_text:
            return []
        kind = self._classify(raw_text)
        if not kind:
            log_raw(
                self.SOURCE_NAME, listing_id=pk_id, county=record.get("county"),
                state="AL", decision="dropped_non_foreclosure",
                reason="neither-tax nor mortgage-foreclosure signal",
                raw_text=raw_text,
                url=f"{self.BASE_URL}/(S({session_id}))/Details.aspx"
                    f"?SID={session_id}&ID={pk_id}",
            )
            return []
        acres = self._extract_acreage(raw_text)
        if acres is not None and acres < config.MIN_ACRES:
            log_raw(
                self.SOURCE_NAME, listing_id=pk_id, county=record.get("county"),
                state="AL", decision="dropped_acres",
                reason=f"acres {acres} below MIN_ACRES {config.MIN_ACRES}",
                raw_text=raw_text,
                url=f"{self.BASE_URL}/(S({session_id}))/Details.aspx"
                    f"?SID={session_id}&ID={pk_id}",
            )
            return None
        address = self._extract_address(raw_text, county)
        auction_date = self._extract_sale_date(raw_text)
        parcel = self._extract_parcel(raw_text)
        detail_url = (f"{self.BASE_URL}/(S({session_id}))/Details.aspx"
                      f"?SID={session_id}&ID={pk_id}")
        log_raw(
            self.SOURCE_NAME, listing_id=pk_id, county=record.get("county"),
            state="AL",
            decision="kept_tax" if kind == "tax_foreclosure" else "kept_mortgage",
            reason="notice classified",
            raw_text=raw_text, url=detail_url,
        )
        prop: PropertyData = {
            "source": self.SOURCE_NAME,
            "source_listing_id": pk_id,
            "url": detail_url,
            "address": address,
            "city": None,
            "county": county,
            "state": "AL",
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": None,
            "acres": acres,
            "description": raw_text[:2000],
            "property_type": kind,
            "image_url": None,
            "auction_date": auction_date,
            "parcel_number": parcel,
            "raw_source_text": raw_text,
            "raw_paragraph": raw_text,
        }
        if address:
            try:
                from .nc_gis_lookup import build_google_maps_url
                prop["google_maps_url"] = build_google_maps_url(
                    None, None, address, None, county, state="AL")
            except Exception:
                pass
        return prop


def scrape_with_enrichment(
    solve_captcha: bool = True,
    enrich: bool = False,
    lookback_days: int = LOOKBACK_DAYS,
) -> List[PropertyData]:
    """Run the AL public-notice scraper.

    Alabama publishes no statewide parcel service (GIS link policy: Maps
    links only), so ``enrich`` is accepted for API parity with the other
    public-notice scrapers but performs no enrichment.
    """
    scraper = ALPublicNoticeScraper(solve_captcha=solve_captcha,
                                    lookback_days=lookback_days)
    return scraper.run()
