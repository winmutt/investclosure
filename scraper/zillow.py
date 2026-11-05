"""Zillow land-listings scraper — county-by-county via Camoufox.

Ported from the legacy realestate app, where Zillow was the top-yield
source (57 qualifying listings per run vs 24 for the runner-up and 0 for
every other source). The original fetched with curl_cffi; this version
fetches through Camoufox (stealth Firefox) per project policy, parsing the
same server-rendered ``__NEXT_DATA__`` JSON payload.

Strategy: loops the qualifying county sets (config.QUALIFYING_COUNTIES),
walks every page of each county's search (see
:func:`build_paged_county_url`), extracts listings from the embedded Next.js
searchResults JSON, applies the price / acreage filters, and rejects
Zillow's cross-state name collisions (e.g. "Perry, KS" served for a Perry
County, KY search). Detail-page enrichment (description + parcel number) is
opt-in via ``fetch_details``.

Pagination matters: a county search renders only 41 listings per page, so
before 2026-11-02 the scraper saw the first 41 of e.g. Macon NC's 820 and
silently missed qualifying properties ordered onto later pages (491 Otter
Mountain Rd — $235k/20.75ac — sat on page 12). Each page is loaded in a
*fresh* Camoufox profile because Zillow walls a session's second
same-origin navigation.
"""
from __future__ import annotations
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Optional

from .base import BaseScraper, PropertyData, camoufox_context
from .config import config, QUALIFYING_COUNTIES
from .geo_lookup import load_index
from .nc_gis_lookup import build_google_maps_url, build_google_maps_topo_url

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.DOTALL)

# County centroids ("<ST>|<county>") used to reject Zillow city-name
# collisions the state guard can't catch (e.g. /homes/macon-nc/ serves the
# town of Macon in Warren County, ~5° from Macon County in the mountains).
_CENTROIDS_PATH = Path(__file__).parent / "county_centroids.json"
MAX_COUNTY_DEG = 0.75  # ~50mi: generous vs county radii, far under city mixups


def load_centroids() -> dict:
    try:
        return json.loads(_CENTROIDS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("County centroid map unavailable: %s", exc)
        return {}


def county_slug(county: str) -> str:
    """'Buncombe' -> 'buncombe'; 'Van Buren'/'van_buren' -> 'van-buren'."""
    return re.sub(r"[\s_]+", "-", (county or "").strip().lower())


def county_display(county: str) -> str:
    """'van_buren' -> 'Van Buren'."""
    return (county or "").replace("_", " ").strip().title()


def build_county_url(county: str, state: str) -> str:
    """Build a Zillow search URL targeting a single county.

    Always uses the ``<county>-county-<state>`` form. The shorter
    ``<county>-<state>`` form is ambiguous whenever a county shares its
    name with a city/town, and Zillow resolves it to the CITY: ``madison-nc``
    served Madison the town (Rockingham Co., ZIP 27025), so every Hot
    Springs / Marshall / Mars Hill listing in Madison COUNTY was absent from
    the results and the centroid guard silently discarded what remained
    ("Madison, NC: 0 listings"). The same collision exists for macon-nc
    (served Macon city, ZIP 27551 in Warren Co.), mitchell, jackson,
    washington, greene and perry. The ``-county-`` form resolves correctly
    for all county shapes verified live — single- and multi-word names
    (``van-buren-county-tn``) and every name collision above.
    """
    return (f"https://www.zillow.com/homes/"
            f"{county_slug(county)}-county-{state.lower()}/")


_FOR_SALE_ROOT = "https://www.zillow.com/homes/for_sale/"


def build_paged_county_url(county: str, state: str, page: int = 1,
                           max_price: int = 0) -> str:
    """Build a *paged* county search URL (legacy ``/homes/for_sale/`` form).

    Only this legacy form accepts an inline filter AND pagination. The modern
    ``/homes/<county>-county-<state>/`` page always renders 41 listings with
    ``filterState: null`` and honours neither ``?searchQueryState=`` nor
    ``filterState``, while

        /homes/for_sale/macon-county-nc/0-250000_price/3_p/

    applies the price cap server-side and returns page 3 *of that filtered
    set* (verified live 2026-11-02: unfiltered page 1 lacks 491 Otter
    Mountain Rd; the price-filtered page 3 has it). Zillow accepts no working
    acreage token on this path, so acreage bounds stay local to
    :func:`parse_county_page`.
    """
    parts = [f"{county_slug(county)}-county-{state.lower()}/"]
    if max_price:
        parts.append(f"0-{int(max_price)}_price/")
    if page > 1:
        parts.append(f"{page}_p/")
    return _FOR_SALE_ROOT + "".join(parts)





def bed_bath_counts(home_info: dict) -> tuple[Optional[int], Optional[float]]:
    """(bedrooms, bathrooms) from Zillow's ``homeInfo``.

    Zillow only carries these for improved properties; land/lot listings
    omit them, in which case both are None. Non-positive values are treated
    as absent so a card never renders "0 bd".
    """
    def _num(value, cast):
        try:
            num = cast(value)
        except (TypeError, ValueError):
            return None
        return num if num > 0 else None

    return (_num(home_info.get("bedrooms"), int),
            _num(home_info.get("bathrooms"), float))


def lot_acres(home_info: dict) -> Optional[float]:
    """Lot size in acres from Zillow's lotAreaValue/lotAreaUnit pair."""
    lot_val = home_info.get("lotAreaValue")
    lot_unit = home_info.get("lotAreaUnit", "")
    if lot_val is None:
        return None
    try:
        val = float(lot_val)
    except (TypeError, ValueError):
        return None
    if lot_unit in ("acres", "ac"):
        return val
    if lot_unit == "sqft":
        return val / 43560.0
    return None


def extract_list_results(html: str) -> Optional[list]:
    """Raw ``listResults`` array from a search page, or None when absent.

    Distinguishing "page has no payload" (bot page / layout change) from
    "page has zero results" (past the last page) is what lets the county
    walk stop for the right reason.
    """
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None
    try:
        return data["props"]["pageProps"]["searchPageState"]["cat1"][
            "searchResults"]["listResults"]
    except (KeyError, TypeError):
        return None


def parse_county_page(html: str, county: str, state: str,
                      max_price: int = 0,
                      min_acres: float = 0.0,
                      centroids: Optional[dict] = None,
                      max_acres: float = 0.0,
                      geo=None,
                      targets: Optional[set] = None) -> list[PropertyData]:
    """Extract qualifying listings from a county search page's __NEXT_DATA__.

    ``max_price`` is in dollars; 0 means no cap. ``centroids`` (see
    :func:`load_centroids`) optionally rejects listings whose coordinates
    sit farther than ``MAX_COUNTY_DEG`` from the searched county's centroid.
    Returns [] when the page carries no parseable search results (robot
    page, layout change, ...).
    """
    return parse_listings(extract_list_results(html) or [], county, state,
                          max_price=max_price, min_acres=min_acres,
                          centroids=centroids, max_acres=max_acres, geo=geo,
                          targets=targets)


def parse_listings(list_results: list, county: str, state: str,
                   max_price: int = 0,
                   min_acres: float = 0.0,
                   centroids: Optional[dict] = None,
                   max_acres: float = 0.0,
                   geo=None,
                   targets: Optional[set] = None) -> list[PropertyData]:
    """Filter one page's raw ``listResults`` items into property records."""
    properties: list[PropertyData] = []
    seen_zpids: set[str] = set()

    for item in list_results:
        zpid = str(item.get("zpid", ""))
        if not zpid or zpid in seen_zpids:
            continue
        seen_zpids.add(zpid)

        home_info = item.get("hdpData", {}).get("homeInfo", {})
        price = item.get("unformattedPrice") or home_info.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        if max_price and price > max_price:
            continue

        acres = lot_acres(home_info)
        if acres is None:
            # Acreage is required to qualify: with no lot size in the
            # payload the 5-acre minimum cannot be verified, and a full
            # paginated county sweep yields thousands of such rows — far
            # more than the post-insert enricher can backfill (2026-11-02:
            # 4,604 harvested, 1,244 with no acreage).
            continue
        if acres < min_acres:
            continue
        if max_acres and acres > max_acres:
            continue

        # Zillow county URLs leak same-named counties from other states
        # (Perry KS served for Perry County KY) — trust Zillow's own state.
        actual_state = (home_info.get("state")
                        or item.get("addressState")
                        or item.get("state") or "").upper()
        if actual_state and actual_state != state.upper():
            logger.warning(
                "Skipping cross-state result for %s, %s: %s (state=%s)",
                county, state, item.get("address", "?"), actual_state)
            continue

        lat_long = item.get("latLong", {}) or home_info
        lat = lat_long.get("latitude")
        lon = lat_long.get("longitude")
        if centroids and lat and lon:
            ref = centroids.get(f"{state.upper()}|{county_slug(county).replace('-', '_')}")
            if ref and (abs(lat - ref[0]) > MAX_COUNTY_DEG
                        or abs(lon - ref[1]) > MAX_COUNTY_DEG):
                logger.warning(
                    "Skipping far-from-county result for %s, %s: %s "
                    "(%.3f,%.3f vs centroid %.3f,%.3f)",
                    county, state, item.get("address", "?"),
                    lat, lon, ref[0], ref[1])
                continue
        city = item.get("addressCity") or home_info.get("city")
        zipcode = (item.get("addressZipcode")
                   or home_info.get("zipcode") or "")
        county_out = county
        if geo is not None:
            resolved = geo.resolve(county, state, city=city,
                                   zip_code=zipcode, targets=targets)
            if resolved is None:
                logger.warning(
                    "Skipping town/ZIP county mismatch for %s, %s: %s "
                    "(%s %s not in any target county)",
                    county, state, item.get("address", "?"), city, zipcode)
                continue
            county_out = resolved
        home_type = home_info.get("homeType", "")
        bedrooms, bathrooms = bed_bath_counts(home_info)
        prop: PropertyData = {
            "source": "zillow",
            "source_listing_id": zpid,
            "url": item.get("detailUrl")
                   or f"https://www.zillow.com/homedetails/{zpid}_zpid/",
            "address": item.get("address") or home_info.get("streetAddress"),
            "city": city,
            "county": county_display(county_out),
            "state": actual_state or state.upper(),
            "zip_code": zipcode,
            "latitude": lat,
            "longitude": lon,
            "price": float(price),
            "acres": round(acres, 2) if acres else None,
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "description": None,
            "parcel_number": None,
            "property_type": home_type.lower() if home_type else "land",
            "image_url": item.get("imgSrc"),
            "google_maps_url": build_google_maps_url(
                lon, lat, item.get("address") or home_info.get("streetAddress"),
                city, county_display(county_out),
                state=actual_state or state.upper()),
            "google_maps_topo_url": build_google_maps_topo_url(
                lon, lat, item.get("address") or home_info.get("streetAddress"),
                city, county_display(county_out),
                state=actual_state or state.upper()),
        }
        properties.append(prop)

    return properties


def extract_detail(html: str) -> tuple[Optional[str], Optional[str]]:
    """Pull (description, parcel_number) from a homedetails page payload."""
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None, None
    try:
        data = json.loads(match.group(1).strip())
        cache_str = (data.get("props", {}).get("pageProps", {})
                     .get("componentProps", {}).get("gdpClientCache", ""))
        if not cache_str:
            return None, None
        cache_data = json.loads(cache_str)
        for value in cache_data.values():
            if isinstance(value, dict) and "property" in value:
                property_data = value["property"]
                description = property_data.get("description")
                parcel = (property_data.get("parcelNumber")
                          or property_data.get("parcelId")
                          or property_data.get("apn")
                          or property_data.get("attribution", {})
                          .get("parcelNumber"))
                return description, parcel
    except (KeyError, json.JSONDecodeError, TypeError, AttributeError):
        pass
    return None, None


class ZillowScraper(BaseScraper):
    """Zillow land/property listings — county-by-county via Camoufox."""

    SOURCE_NAME = "zillow"
    BASE_URL = "https://www.zillow.com"
    PAGE_TIMEOUT_MS = 60000
    DELAY_BETWEEN_DETAIL_PAGES = (1.0, 2.0)
    DELAY_BETWEEN_SEARCH_PAGES = (1.0, 2.0)
    BOT_PAGE_COOLDOWN = (10.0, 18.0)
    MAX_CONSECUTIVE_CHALLENGES = 12
    # Zillow renders up to 41 listings per search page; a short page is
    # therefore the last page (saves a wasted request in small counties).
    PAGE_SIZE = 41
    # Zillow's own search caps out at 20 pages of 41 listings; the price
    # filter keeps almost every county below that (Macon NC: 20 pages
    # unfiltered -> 12 with max_price=250k).
    MAX_PAGES_PER_COUNTY = 20

    def __init__(self, fetch_details: bool = False,
                 states: Optional[list[str]] = None):
        super().__init__(delay_range=config.get_delay_range("zillow"))
        self.max_price = config.get_max_price("zillow")
        self.min_acres = config.get_min_acres("zillow")
        self.max_acres = config.get_max_acres("zillow")
        self.fetch_details = fetch_details
        self.states = [s.upper() for s in states] if states else None
        self.centroids = load_centroids()
        self.geo = load_index()

    def scrape(self) -> list[PropertyData]:
        """Scrape all qualifying counties, state by state.

        Zillow's PerimeterX flags a browser session after a handful of
        same-origin navigations, so every search page gets a FRESH Camoufox
        profile (matching the legacy curl_cffi-per-request recipe that
        produced 57 listings/run). A circuit breaker aborts the sweep when
        Zillow starts serving bot pages for consecutive counties (IP-level
        flag).
        """
        all_props: list[PropertyData] = []
        state_map = {s: c for s, c in QUALIFYING_COUNTIES.items()
                     if not self.states or s in self.states}
        total = sum(len(c) for c in state_map.values())
        logger.info("ZILLOW: %d counties, max_price=%s, min_acres=%s, "
                    "details=%s", total,
                    self.max_price or "none", self.min_acres,
                    self.fetch_details)

        for state, counties in state_map.items():
            if self._sweep_state(state, counties, all_props):
                break

        if self.fetch_details and all_props:
            self._enrich_details(all_props)

        unique = self._dedupe(all_props)
        logger.info("ZILLOW done: %d raw → %d unique", len(all_props),
                    len(unique))
        return unique

    def _sweep_state(self, state: str, counties: list[str],
                     all_props: list[PropertyData]) -> bool:
        """Scrape every county in one state into all_props.

        Returns True when the circuit breaker tripped (caller stops)."""
        logger.info("── %s (%d counties) ──", state, len(counties))
        consecutive = 0
        for county in counties:
            props, challenged, returned, pages = self._scrape_county(county,
                                                                     state)
            logger.info("  %s, %s: %d returned / %d qualifying "
                        "(%d page%s)%s", county_display(county), state,
                        returned, len(props), pages,
                        "" if pages == 1 else "s",
                        " [bot page]" if challenged else "")
            all_props.extend(props)
            consecutive = consecutive + 1 if challenged else 0
            if consecutive >= self.MAX_CONSECUTIVE_CHALLENGES:
                logger.error(
                    "ZILLOW: %d consecutive bot pages — aborting sweep; "
                    "egress IP likely flagged by Zillow",
                    consecutive)
                return True
        return False

    def _scrape_county(self, county: str, state: str,
                       attempts: int = 2
                       ) -> tuple[list[PropertyData], bool, int, int]:
        """Walk every page of one county's search.

        Returns ``(properties, challenged, returned, pages)``: qualifying
        records, whether page 1 was a bot page, the raw listing count seen
        across all pages (so short/failed walks are visible in the log), and
        the number of pages walked.

        A fresh Camoufox profile is launched per page — Zillow walls the
        second same-origin navigation in a session, so reusing one browser
        caps a county at 41 listings.
        """
        props: list[PropertyData] = []
        seen: set[str] = set()
        seen_raw: set[str] = set()
        returned = 0
        pages = 0
        challenged = False
        targets = set(QUALIFYING_COUNTIES.get(state.upper(), []))
        page_no = 1

        while page_no <= self.MAX_PAGES_PER_COUNTY:
            url = build_paged_county_url(county, state, page_no,
                                         self.max_price)
            html = self._fetch_page(url, county, state, page_no,
                                    attempts if page_no == 1 else 1)
            if html is None:
                if page_no == 1:
                    challenged = True
                    logger.warning(
                        "No __NEXT_DATA__ for %s (%s) after %d attempts — "
                        "bot page or layout change?", county, state, attempts)
                else:
                    logger.info(
                        "  %s, %s: page %d unavailable — keeping %d "
                        "listing(s) from %d page(s)",
                        county_display(county), state, page_no, len(props),
                        pages)
                break

            items = extract_list_results(html)
            if not items:
                break  # past the last page (or a payload-less page)

            page_props = parse_listings(
                items, county, state,
                max_price=self.max_price, min_acres=self.min_acres,
                centroids=self.centroids, max_acres=self.max_acres,
                geo=self.geo, targets=targets)

            new_raw = 0
            for item in items:
                zpid = str(item.get("zpid", ""))
                if zpid and zpid not in seen_raw:
                    seen_raw.add(zpid)
                    new_raw += 1
            for prop in page_props:
                pid = prop.get("source_listing_id", "")
                if pid and pid not in seen:
                    seen.add(pid)
                    props.append(prop)
            returned += len(items)
            pages += 1

            if not new_raw:
                # The same 41 zpids again: Zillow ignored the page offset.
                logger.info("  %s, %s: page %d repeated earlier results — "
                            "stopping", county_display(county), state, page_no)
                break
            if len(items) < self.PAGE_SIZE:
                break  # short page == last page
            page_no += 1
            time.sleep(random.uniform(*self.DELAY_BETWEEN_SEARCH_PAGES))

        if pages >= self.MAX_PAGES_PER_COUNTY and returned >= (
                self.PAGE_SIZE * self.MAX_PAGES_PER_COUNTY):
            logger.warning(
                "  %s, %s: hit the %d-page cap — some listings unreachable",
                county_display(county), state, self.MAX_PAGES_PER_COUNTY)
        return props, challenged, returned, pages

    def _fetch_page(self, url: str, county: str, state: str, page_no: int,
                    attempts: int = 1) -> Optional[str]:
        """Load *url* in a fresh Camoufox profile; return HTML or None."""
        for attempt in range(1, attempts + 1):
            self._random_delay()
            html = ""
            try:
                with camoufox_context() as page:
                    page.set_default_timeout(self.PAGE_TIMEOUT_MS)
                    resp = page.goto(url, wait_until="domcontentloaded",
                                     timeout=self.PAGE_TIMEOUT_MS)
                    html = resp.text() if resp else ""
            except Exception as exc:
                logger.warning("County page fetch failed (%s, %s page %d): %s",
                               county, state, page_no, exc)
            if "__NEXT_DATA__" in (html or ""):
                return html
            if attempt < attempts:
                cooldown = random.uniform(*self.BOT_PAGE_COOLDOWN)
                logger.info("Bot page for %s (%s) — cooling down %.0fs, "
                            "retrying", county, state, cooldown)
                time.sleep(cooldown)
        return None

    def _enrich_details(self, props: list[PropertyData]) -> None:
        logger.info("Fetching %d detail pages", len(props))
        for i, prop in enumerate(props, 1):
            url = prop.get("url")
            if not url:
                continue
            time.sleep(random.uniform(*self.DELAY_BETWEEN_DETAIL_PAGES))
            try:
                with camoufox_context() as page:
                    page.set_default_timeout(self.PAGE_TIMEOUT_MS)
                    resp = page.goto(url, wait_until="domcontentloaded",
                                     timeout=self.PAGE_TIMEOUT_MS)
                    desc, parcel = extract_detail(resp.text() if resp else "")
            except Exception as exc:
                logger.warning("Detail fetch failed for %s: %s", url, exc)
                continue
            if desc:
                prop["description"] = desc
            if parcel:
                prop["parcel_number"] = parcel

    @staticmethod
    def _dedupe(props: list[PropertyData]) -> list[PropertyData]:
        seen: set[str] = set()
        unique: list[PropertyData] = []
        for p in props:
            pid = p.get("source_listing_id", "")
            if pid and pid not in seen:
                seen.add(pid)
                unique.append(p)
        return unique


def scrape_all() -> list[PropertyData]:
    """Shortcut: instantiate and run the scraper."""
    return ZillowScraper().run()


def enforce_thresholds() -> dict:
    """Archive active zillow rows that violate the CURRENT threshold set.

    Thresholds can tighten after rows were scraped (and enrichment can
    overwrite listing acreage with smaller survey values), so every zillow
    run reaps stragglers: price above the cap, acreage outside
    [min_acres, max_acres], or NULL acreage (unverifiable, and the scrape
    itself now requires a lot size). Writes the audit fields the dashboard
    uses, so reaped rows are traceable. Returns archived/kept counts."""
    import sqlite3
    min_acres = config.get_min_acres("zillow")
    max_price = config.get_max_price("zillow")
    max_acres = config.get_max_acres("zillow")
    conn = sqlite3.connect(str(config.db_path))
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conds, params = [], []
        if max_price:
            conds.append("(price_cents IS NOT NULL AND price_cents > ?)")
            params.append(int(max_price) * 100)
        acres_cond = "acres < ?"
        params_a: list = [min_acres]
        if max_acres:
            acres_cond += " OR acres > ?"
            params_a.append(max_acres)
        conds.append(f"(acres IS NULL OR {acres_cond})")
        params += params_a
        cur = conn.execute(
            "UPDATE properties SET status = 'archived', "
            "archived_at = datetime('now'), archived_by = 'system', "
            "archive_reason = 'threshold_reap' "
            "WHERE source = 'zillow' AND status = 'active' AND ("
            + " OR ".join(conds) + ")", params)
        conn.commit()
        archived = cur.rowcount
        kept = conn.execute("SELECT COUNT(*) FROM properties "
                            "WHERE source = 'zillow' "
                            "AND status = 'active'").fetchone()[0]
        logger.info("zillow threshold reap: archived=%d active_kept=%d",
                    archived, kept)
        return {"archived": archived, "kept": kept}
    finally:
        conn.close()
