"""Zillow land-listings scraper — county-by-county via Camoufox.

Ported from the legacy realestate app, where Zillow was the top-yield
source (57 qualifying listings per run vs 24 for the runner-up and 0 for
every other source). The original fetched with curl_cffi; this version
fetches through Camoufox (stealth Firefox) per project policy, parsing the
same server-rendered ``__NEXT_DATA__`` JSON payload.

Strategy: one Camoufox session loops the qualifying county sets
(config.QUALIFYING_COUNTIES), loads
``https://www.zillow.com/homes/<county>-<state>/`` per county, extracts
listings from the embedded Next.js searchResults JSON, applies the price /
acreage filters, and rejects Zillow's cross-state name collisions
(e.g. "Perry, KS" served for a Perry County, KY search). Detail-page
enrichment (description + parcel number) is opt-in via ``fetch_details``.
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
    """Build a Zillow search URL targeting a single county."""
    return f"https://www.zillow.com/homes/{county_slug(county)}-{state.lower()}/"


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


def parse_county_page(html: str, county: str, state: str,
                      max_price: int = 0,
                      min_acres: float = 0.0,
                      centroids: Optional[dict] = None) -> list[PropertyData]:
    """Extract qualifying listings from a county search page's __NEXT_DATA__.

    ``max_price`` is in dollars; 0 means no cap. ``centroids`` (see
    :func:`load_centroids`) optionally rejects listings whose coordinates
    sit farther than ``MAX_COUNTY_DEG`` from the searched county's centroid.
    Returns [] when the page carries no parseable search results (robot
    page, layout change, ...).
    """
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return []

    try:
        list_results = (
            data["props"]["pageProps"]["searchPageState"]
            ["cat1"]["searchResults"]["listResults"]
        )
    except (KeyError, TypeError):
        return []

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
        if acres is not None and acres < min_acres:
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
        home_type = home_info.get("homeType", "")
        prop: PropertyData = {
            "source": "zillow",
            "source_listing_id": zpid,
            "url": item.get("detailUrl")
                   or f"https://www.zillow.com/homedetails/{zpid}_zpid/",
            "address": item.get("address") or home_info.get("streetAddress"),
            "city": item.get("addressCity") or home_info.get("city"),
            "county": county_display(county),
            "state": actual_state or state.upper(),
            "zip_code": item.get("addressZipcode") or home_info.get("zipcode"),
            "latitude": lat,
            "longitude": lon,
            "price": float(price),
            "acres": round(acres, 2) if acres else None,
            "description": None,
            "parcel_number": None,
            "property_type": home_type.lower() if home_type else "land",
            "image_url": item.get("imgSrc"),
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

    def __init__(self, fetch_details: bool = False,
                 states: Optional[list[str]] = None):
        super().__init__(delay_range=config.get_delay_range("zillow"))
        self.max_price = config.get_max_price("zillow")
        self.min_acres = config.get_min_acres("zillow")
        self.fetch_details = fetch_details
        self.states = [s.upper() for s in states] if states else None
        self.centroids = load_centroids()

    def scrape(self) -> list[PropertyData]:
        """Scrape all qualifying counties, state by state."""
        all_props: list[PropertyData] = []
        state_map = {s: c for s, c in QUALIFYING_COUNTIES.items()
                     if not self.states or s in self.states}
        total = sum(len(c) for c in state_map.values())
        logger.info("ZILLOW: %d counties, max_price=%s, min_acres=%s, "
                    "details=%s", sum(len(c) for c in state_map.values()),
                    self.max_price or "none", self.min_acres,
                    self.fetch_details)

        try:
            with camoufox_context() as page:
                page.set_default_timeout(self.PAGE_TIMEOUT_MS)
                for state, counties in state_map.items():
                    logger.info("── %s (%d counties) ──", state, len(counties))
                    for county in counties:
                        props = self._scrape_county(page, county, state)
                        logger.info("  %s, %s: %d listings",
                                    county_display(county), state, len(props))
                        all_props.extend(props)

                if self.fetch_details and all_props:
                    self._enrich_details(page, all_props)
        except Exception as exc:
            logger.error("Zillow fetch failed: %s", exc, exc_info=True)

        unique = self._dedupe(all_props)
        logger.info("ZILLOW done: %d raw → %d unique", len(all_props),
                    len(unique))
        return unique

    def _scrape_county(self, page, county: str, state: str
                       ) -> list[PropertyData]:
        url = build_county_url(county, state)
        self._random_delay()
        try:
            resp = page.goto(url, wait_until="domcontentloaded",
                              timeout=self.PAGE_TIMEOUT_MS)
            html = resp.text() if resp else ""
        except Exception as exc:
            logger.warning("County page fetch failed (%s, %s): %s",
                           county, state, exc)
            return []
        props = parse_county_page(html, county, state,
                                  max_price=self.max_price,
                                  min_acres=self.min_acres,
                                  centroids=self.centroids)
        if not props and "__NEXT_DATA__" not in (html or ""):
            logger.warning("No __NEXT_DATA__ on %s (%s) — bot page or "
                           "layout change?", county, state)
        return props

    def _enrich_details(self, page, props: list[PropertyData]) -> None:
        logger.info("Fetching %d detail pages", len(props))
        for i, prop in enumerate(props, 1):
            url = prop.get("url")
            if not url:
                continue
            time.sleep(random.uniform(*self.DELAY_BETWEEN_DETAIL_PAGES))
            try:
                resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=self.PAGE_TIMEOUT_MS)
                desc, parcel = extract_detail(resp.text() if resp else "")
                if desc:
                    prop["description"] = desc
                if parcel:
                    prop["parcel_number"] = parcel
            except Exception as exc:
                logger.warning("Detail fetch failed for %s: %s", url, exc)

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
