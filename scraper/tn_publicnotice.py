"""TN Public Notice foreclosure scraper — tnpublicnotice.com via Camoufox.

Turnstile challenges are solved in-browser by the camoufox page itself
(no third-party solving service).


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
    normalize_notice_text,
)
from .rawlog import log_raw

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


# TN county-trustee tax-sale notices are published as a single notice containing
# a *table* of delinquent parcels. Each parcel row ends with a "Total:$<amount>"
# marker; we split the notice into one listing per parcel (mirroring
# ga_publicnotice's bundled-notice handling).
_TN_TOTAL_RE = re.compile(r"Total:\s*\$[\d,]+\.\d{2}")
_TN_PARCEL_ID_RE = re.compile(r"(\d{2,3}-[A-Za-z]/[A-Za-z\d]+/\d+\.\d{2})")
_TN_ADDR_RE = re.compile(
    r"(\d{1,5}\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+){0,4}\s+(?:STREET|ST|AVENUE|"
    r"AVE|BOULEVARD|BLVD|DRIVE|DR|ROAD|RD|LANE|LN|HIGHWAY|HWY|COURT|CT|CIRCLE|"
    r"CIR|PARKWAY|PKWY|PIKE|WAY|TRAIL)\.?)",
    re.IGNORECASE,
)
_TN_ADDR_NO_NUM_RE = re.compile(
    r"([A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+){0,3}\s+(?:STREET|ST|AVENUE|AVE|"
    r"BOULEVARD|BLVD|DRIVE|DR|ROAD|RD|LANE|LN|HIGHWAY|HWY|COURT|CT|CIRCLE|CIR|"
    r"PARKWAY|PKWY|PIKE|WAY|TRAIL)\.?)",
    re.IGNORECASE,
)
_TN_SALE_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE,
)
# Day-first variant: "on the 7th day of October, 2026".
_TN_SALE_DATE_RE2 = re.compile(
    r"(?:on\s+the\s+)?(\d{1,2})(?:st|nd|rd|th)\s+day\s+of\s+"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December),?\s+(\d{4})",
    re.IGNORECASE,
)
_TN_ACRES_RE = re.compile(r"(\d[\d,]*\.?\d*|\.\d+)\s*(?:acres?|acs?|AC)\b",
                             re.IGNORECASE)
# Acreage stated for the granted tract ("containing…", "having an area of…").
_GRANT_ACRES_RE = re.compile(
    r"containing|having an area of|with an area of|comprising|"
    r"consisting of|embracing", re.IGNORECASE)
# Acreage carved back out (exceptions, reservations, non-encumbered slivers).
_EXCEPT_ACRES_RE = re.compile(
    r"except|exclud|does not encumber|reserving|reservation|subject to",
    re.IGNORECASE)
# Sale-venue language — an address here is where the auction happens
# (often the courthouse), NOT the property being sold.
_VENUE_RES = ("courthouse", "court house", "front door", "main door",
              "court door", "in front of")
# Verbs anchoring the actual auction date (vs deed/loan/modification dates).
_SALE_VERBS = ("offer for sale", "offered for sale", "will sell",
               "sell at public", "sale date", "date of sale", "auction",
               "trustee's sale", "substitute trustee", "sale")


# Phrases that open the legal description — an address after one of these
# is the property even when sale-venue language appears nearby (the sale
# terms flow straight into "...the following described tract: ... known
# as Warren Lane").
_DESCRIPTION_BOUNDARY_RE = re.compile(
    r"following described|described as|to-wit|situate|lying and being|"
    r"more particularly|tract no\.?", re.IGNORECASE)


def _in_venue_context(text: str, pos: int, window: int = 150) -> bool:
    before = text[max(0, pos - window):pos]
    if not any(k in before.lower() for k in _VENUE_RES):
        return False
    # A legal-description boundary between the venue language and the
    # address means we've moved on to the property itself.
    return _DESCRIPTION_BOUNDARY_RE.search(before) is None


def _tn_extract_sale_date(text: str):
    """Auction date: prefer dates near sale verbs over deed/loan dates.

    Deed-of-trust recitals lead with old dates ("Deed of Trust dated June
    7, 2008"); the auction date sits by sale verbs ("offer for sale ...
    on the 7th day of October, 2026"). Highest score wins, ties go to the
    later occurrence (continuances read later).
    """
    if not text:
        return None
    cands = []
    for m in _TN_SALE_DATE_RE.finditer(text):
        cands.append((m, m.group(1), m.group(2), m.group(3)))
    for m in _TN_SALE_DATE_RE2.finditer(text):
        cands.append((m, m.group(2), m.group(1), m.group(3)))
    scored = []
    for m, mon_name, day, year in cands:
        mon = _MONTHS.get(mon_name.title())
        if not mon:
            continue
        try:
            iso = datetime.date(int(year), mon, int(day)).isoformat()
        except Exception:
            continue
        ctx = text[max(0, m.start() - 120):m.end() + 120].lower()
        score = sum(1 for v in _SALE_VERBS if v in ctx)
        scored.append((score, m.start(), iso))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[-1][2]


# Measurement words mark metes-and-bounds course fragments ("250 feet of
# Warren Lane", "95.89 feet"), never street addresses.
_JUNK_ADDR_RE = re.compile(
    r"\b(feet|foot|miles?|chains?|poles?|rods?|perches?|acres?)\b",
    re.IGNORECASE)


def _tn_extract_address(block: str):
    if not block:
        return None

    def _usable(m):
        text = m.group(1)
        return (not _JUNK_ADDR_RE.search(text)
                and not _in_venue_context(block, m.start()))

    # The subject property's own name first ("tract ... known as Warren
    # Lane") — deed fragments elsewhere in the notice ("estate is 200
    # Warren Lane", metes courses) must not outrank it.
    known = re.search(r"known as\s+(" + _TN_ADDR_NO_NUM_RE.pattern + r")",
                      block, re.IGNORECASE)
    if known:
        addr = known.group(1).strip().rstrip(",. ")[:120]
        if addr and not _JUNK_ADDR_RE.search(addr) and not _in_venue_context(
                block, known.start(1)):
            return addr
    # Then the *last* surviving numbered address: each parcel row ends with
    # a "Total:$" marker, so the parcel's own street address sits near the
    # end of its block (the leading preamble, e.g. "2026 IN THE CHANCERY
    # COURT", would otherwise be matched first). Venue matches ("in front
    # of the ... Courthouse, 8095 Rutledge Pike") name the auction site.
    for m in reversed(list(_TN_ADDR_RE.finditer(block))):
        if _usable(m):
            return m.group(1).strip()[:120]
    for m in reversed(list(_TN_ADDR_NO_NUM_RE.finditer(block))):
        if _usable(m):
            return m.group(1).strip()[:120]
    return None


def _tn_parse_acres(block: str):
    """Granted-tract acreage; skips exception-clause slivers.

    Legal descriptions often state the sold area ("having an area of 0.74
    acres") AND carve-outs ("does not encumber the premises (.95 acres)").
    Prefer grant-context mentions; leading-decimal values (".95" -> 0.95)
    parse correctly; exception-only text yields None.
    """
    if not block:
        return None
    cands = []
    for m in _TN_ACRES_RE.finditer(block):
        try:
            v = float(m.group(1).replace(",", ""))
        except Exception:
            continue
        if not (0.1 < v < 10000):
            continue
        ctx = block[max(0, m.start() - 80):m.start()]
        if _EXCEPT_ACRES_RE.search(ctx):
            rank = 2
        elif _GRANT_ACRES_RE.search(ctx):
            rank = 0
        else:
            rank = 1
        cands.append((rank, m.start(), v))
    if not cands:
        return None
    cands.sort(key=lambda t: (t[0], t[1]))
    if cands[0][0] == 2:
        return None
    return cands[0][2]


def _tn_parse_parcels(text: str, county: str, auction_date: str, detail_url: str,
                      kind: str = "tax_foreclosure"):
    """Split a consolidated TN tax-sale notice into per-parcel listings.

    Each parcel row in the delinquent-tax table terminates with a
    ``Total:$<amount>`` marker; we slice the notice into one block per parcel
    and extract address / parcel number / acres from each block. Returns an
    empty list when the notice has no parcel table (caller falls back to a
    single consolidated record). ``kind`` tags the parcel rows
    (``tax_foreclosure`` or ``mortgage_foreclosure``).
    """
    if not text:
        return []
    # The PDF text is frequently delivered space-stripped (e.g.
    # "NOTICEOFSULLIVANCOUNTY..."), which breaks the whitespace-dependent
    # address regexes below — re-insert spaces before splitting.
    text = normalize_notice_text(text)
    matches = list(_TN_TOTAL_RE.finditer(text))
    if not matches:
        return []

    county = (county or "").lower().strip()
    blocks = [text[: matches[0].start()]]  # first parcel + preamble
    for i in range(1, len(matches)):
        blocks.append(text[matches[i - 1].end(): matches[i].start()])

    # Pre-import URL builders (avoid circular imports at module load)
    try:
        from .gis_urls import get_tn_gis_url
        from .nc_gis_lookup import (
            build_google_maps_url,
            build_google_maps_topo_url,
            build_satellite_url,
            build_street_view_url,
        )
        _has_urls = True
    except Exception:
        _has_urls = False

    parcels: List[PropertyData] = []
    for blk in blocks:
        if not blk.strip():
            continue
        address = _tn_extract_address(blk)
        pid_m = _TN_PARCEL_ID_RE.search(blk)
        parcel_no = pid_m.group(1).strip() if pid_m else None
        acres = _tn_parse_acres(blk)
        key = parcel_no or address
        if not key:
            # No stable identifier (no parcel id and no street address) — skip
            # rather than emit an un-keyed, non-dedupable row.
            continue
        desc = blk.strip()
        prop: PropertyData = {
            "source": "tn_publicnotice",
            "source_listing_id": f"{county}:{key}",
            "url": detail_url,
            "address": address,
            "city": None,
            "county": county,
            "state": "TN",
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": 1,
            "acres": acres,
            "description": desc,
            "property_type": kind,
            "auction_date": auction_date,
            "parcel_number": parcel_no,
            "raw_source_text": desc,
            "raw_paragraph": desc,
        }
        if _has_urls and address:
            try:
                prop["google_maps_url"] = build_google_maps_url(None, None, address, None, county, state="TN")
                prop["google_maps_topo_url"] = build_google_maps_topo_url(None, None, address, None, county, state="TN")
                prop["google_maps_satellite_url"] = build_satellite_url(None, None, address, None, county, state="TN")
                prop["google_maps_street_url"] = build_street_view_url(None, None, address, None, county, state="TN")
                prop["gis_url"] = get_tn_gis_url(county, parcel_no or "")
            except Exception:
                pass
        parcels.append(prop)
    return parcels


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
        t = normalize_notice_text(text).upper()
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
                        if not self._goto_next_page(page, cur + 1):
                            break
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

                # Pre-filter: court *service* publications are dropped so we
                # don't burn a Turnstile solve on them. Mortgage/deed-of-trust
                # (bank) foreclosures are KEPT for the Mtg tab — only the
                # authoritative detail-level check below decides tax vs mtg.
                pre_filtered = []
                mortgage_kept = 0
                publication_skipped = 0
                for r in target_records:
                    grid_text = r.get("full_text") or ""
                    if self._is_publication_notice(grid_text):
                        publication_skipped += 1
                        log_raw(
                            self.SOURCE_NAME,
                            listing_id=r.get("sp_case") or r.get("pk_id"),
                            county=r.get("county"), state="TN",
                            decision="dropped_publication",
                            reason="court service publication (no parcel/auction)",
                            raw_text=grid_text,
                        )
                    else:
                        if self._is_mortgage_foreclosure(grid_text):
                            mortgage_kept += 1
                        pre_filtered.append(r)
                target_records = pre_filtered
                print(f"  {mortgage_kept} mortgage/bank foreclosures kept for Mtg tab; "
                      f"{publication_skipped} dropped as court publications; "
                      f"{len(target_records)} candidate notices remain")

                print(f"  [4/4] Extracting details ({len(target_records)} cases) ...")
                for i, rec in enumerate(target_records):
                    print(f"    [{i+1}/{len(target_records)}] {rec['sp_case'] or rec['pk_id']} - {rec.get('county', '?')}",
                          end=" ", flush=True)
                    props = self._extract_detail(page, session_id, rec)
                    if props:
                        print(f"-> {len(props)} qualifying parcel(s)")
                        properties.extend(props)
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

    def _extract_detail(self, page, session_id: str, record: dict) -> List[PropertyData]:
        """Navigate to detail page, pass the Turnstile gate, extract notice text.

        County-trustee tax-sale notices are consolidated tables of delinquent
        parcels, so we return one listing per parcel (see ``_tn_parse_parcels``).
        """
        pk_id = record["pk_id"]
        raw_text = self._extract_notice_text(page, session_id, pk_id)
        if not raw_text:
            return []

        # Authoritative classification on the full notice text: genuine
        # county-trustee tax sales -> Tax tab; mortgage/deed-of-trust (bank)
        # sales -> Mtg tab; anything else is dropped.
        if self._is_tax_foreclosure(raw_text):
            kind = "tax_foreclosure"
        elif self._is_mortgage_foreclosure(raw_text):
            kind = "mortgage_foreclosure"
        else:
            log_raw(
                self.SOURCE_NAME,
                listing_id=record.get("sp_case") or pk_id,
                county=record.get("county"), state="TN",
                decision="dropped_non_foreclosure",
                reason="neither tax-sale nor mortgage-foreclosure signal",
                raw_text=raw_text,
                url=f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}",
            )
            logger.info("Dropping non-foreclosure %s", pk_id)
            return []

        # Reject court *service* publications (non-resident / cannot-be-located
        # delinquent-taxpayer lists) even though they mention "delinquent tax" —
        # they name dozens of parties with no single parcel or auction.
        if self._is_publication_notice(raw_text):
            log_raw(
                self.SOURCE_NAME,
                listing_id=record.get("sp_case") or pk_id,
                county=record.get("county"), state="TN",
                decision="dropped_publication",
                reason="court service publication (non-resident service list)",
                raw_text=raw_text,
                url=f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}",
            )
            logger.info("Dropping court publication %s (non-resident service list)", pk_id)
            return []

        auction_date = _tn_extract_sale_date(raw_text)
        county = (record.get("county") or "").lower().strip()
        detail_url = f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}"

        # Source visibility: the on-page HTML is truncated to 1,000 chars
        # ("Web display limited") — full text means the PDF was grabbed.
        text_src = ("html-fallback" if "Web display limited" in raw_text
                    else "pdf-source")
        parcels = _tn_parse_parcels(raw_text, county, auction_date, detail_url, kind=kind)
        if parcels:
            log_raw(
                self.SOURCE_NAME,
                listing_id=record.get("sp_case") or pk_id,
                county=record.get("county"), state="TN",
                decision="kept_tax" if kind == "tax_foreclosure" else "kept_mortgage",
                reason=f"{len(parcels)} parcel(s) split [{text_src}]",
                raw_text=raw_text, url=detail_url,
            )
            return parcels

        # Fallback: single consolidated record (no parseable parcel table).
        # Mortgage trustee sales are single-property notices, so they land here.
        acres = self._extract_acreage(raw_text)
        if acres is not None and acres < config.MIN_ACRES:
            log_raw(
                self.SOURCE_NAME,
                listing_id=record.get("sp_case") or pk_id,
                county=record.get("county"), state="TN",
                decision="dropped_acres",
                reason=f"acres {acres} below MIN_ACRES {config.MIN_ACRES}",
                raw_text=raw_text, url=detail_url,
            )
            return []
        address = extract_street_address(raw_text)
        prop: PropertyData = {
            "source": self.SOURCE_NAME,
            "source_listing_id": record.get("sp_case") or pk_id,
            "url": detail_url,
            "address": address,
            "city": None,
            "county": county,
            "state": "TN",
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": 1,
            "acres": acres,
            "description": raw_text[:2000],
            "property_type": kind,
            "image_url": None,
            "auction_date": auction_date,
            "raw_source_text": raw_text,
            "raw_paragraph": raw_text,
        }
        log_raw(
            self.SOURCE_NAME,
            listing_id=record.get("sp_case") or pk_id,
            county=record.get("county"), state="TN",
            decision="kept_tax" if kind == "tax_foreclosure" else "kept_mortgage",
            reason=f"single consolidated record [{text_src}]",
            raw_text=raw_text, url=detail_url,
        )
        if address:
            try:
                from .gis_urls import get_tn_gis_url
                from .nc_gis_lookup import (
                    build_google_maps_url,
                    build_google_maps_topo_url,
                    build_satellite_url,
                    build_street_view_url,
                )
                prop["google_maps_url"] = build_google_maps_url(None, None, address, None, county, state="TN")
                prop["google_maps_topo_url"] = build_google_maps_topo_url(None, None, address, None, county, state="TN")
                prop["google_maps_satellite_url"] = build_satellite_url(None, None, address, None, county, state="TN")
                prop["google_maps_street_url"] = build_street_view_url(None, None, address, None, county, state="TN")
                prop["gis_url"] = get_tn_gis_url(county, "")
            except Exception:
                pass
        return [prop]


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
