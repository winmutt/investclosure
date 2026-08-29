"""TNMap Assessment scraper — tnmap.tn.gov GIS property assessment data.

Enriches TN foreclosure data with property assessment information from
the Tennessee Department of Revenue's GIS portal (tnmap.tn.gov).

Architecture:
    Playwright (headless Chromium) -> tnmap.tn.gov -> Cartograph API
    Cartograph session -> ArcGIS token -> ArcGIS MapServer queries

Returns assessed values, owner names, acreage (DEEDAC/CALCAC), and parcel IDs
that can be cross-referenced with foreclosure listings.
"""
from __future__ import annotations
import json
import re
import logging
import time
from typing import Optional

from .base import camoufox_context
from .config import config, TN_FORECLOSURE_COUNTIES

logger = logging.getLogger(__name__)

TNMAP_BASE_URL = "https://tnmap.tn.gov"
TNMAP_ASSESSMENT_URL = f"{TNMAP_BASE_URL}/assessment"

# County names from TN map config (alphabetical order, 1-indexed)
# 95 TN counties — matching TN_FORECLOSURE_COUNTIES
TN_COUNTIES = [
    "anderson", "bedford", "benton", "bledsoe", "blount",
    "bradley", "campbell", "cannon", "carroll", "carter",
    "cheatham", "chester", "claiborne", "clay", "cocke",
    "coffee", "crockett", "cumberland", "davidson", "decatur",
    "dekalb", "dickson", "dyer", "fayette", "fentress",
    "franklin", "gibson", "giles", "grainger", "greene",
    "grundy", "hamblen", "hamilton", "hancock", "hardeman",
    "hardin", "hawkins", "haywood", "henderson", "henry",
    "hickman", "houston", "humphreys", "jackson", "jefferson",
    "johnson", "knox", "lake", "lauderdale", "lawrence",
    "lewis", "lincoln", "loudon", "macon", "madison",
    "marion", "marshall", "maury", "mcminn", "mcnairy",
    "meigs", "monroe", "montgomery", "moore", "morgan",
    "obion", "overton", "perry", "pickett", "polk",
    "putnam", "rhea", "roane", "robertson", "rutherford",
    "scott", "sequatchie", "sevier", "shelby", "smith",
    "stewart", "sullivan", "sumner", "tipton", "trousdale",
    "unicoi", "union", "vanburen", "warren", "washington",
    "wayne", "weakley", "white", "williamson", "wilson",
]


class TNMapScraper:
    """Scraper for TN property assessment data from tnmap.tn.gov.

    Enriches foreclosure listing data with:
    - Assessed owner name (OWNER, OWNER2)
    - Property address and city
    - Deeded acreage (DEEDAC)
    - Calculated acreage (CALCAC)
    - Parcel numbers (GISLINK, CMAP, GP, PARCEL)
    """

    SOURCE_NAME = "tnmap"

    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self._cartograph_config = None
        self._token = None

    def _get_token(self, page) -> tuple[dict, str]:
        """Get cartograph config and ArcGIS token from the browser session."""
        cartograph_url = f"{TNMAP_BASE_URL}/cms/cartographs"
        responses = []

        def handle_response(response):
            # Match by URL substring (method can be POST or GET)
            if "/cms/cartographs" in response.url:
                responses.append(response)

        page.on("response", handle_response)
        try:
            # Trigger cartograph session (listener must be attached first)
            page.goto(TNMAP_ASSESSMENT_URL + "/", wait_until="networkidle", timeout=30000)
            time.sleep(2)
            page.wait_for_timeout(3000)

            if not responses:
                logger.warning("No cartograph response on first load; reloading")
                page.reload(wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)
        finally:
            # Camoufox's Page exposes `on` but not `off`; remove defensively.
            if hasattr(page, "off"):
                page.off("response", handle_response)
            elif hasattr(page, "remove_listener"):
                page.remove_listener("response", handle_response)

        if not responses:
            raise RuntimeError("Could not capture cartograph session response")

        response = responses[0]
        body = response.body()
        if hasattr(body, 'decode'):
            body = body.decode('utf-8', errors='replace')

        config_data = json.loads(body)
        sources = config_data.get("sources", {})
        assessment_props = sources.get("assessmentProperties", {})

        token = assessment_props.get("token", "")
        # NOTE: the ArcGIS token is valid WITH its trailing "." — do not strip it.

        return config_data, token

    def _query_parcels_by_county(
        self,
        page,
        county_id: int,
        max_records: int = 100,
    ) -> list[dict]:
        """Query ArcGIS parcel layer for a given county ID."""
        query_url = (
            f"{TNMAP_BASE_URL}/arcgis/rest/services/CADASTRAL/"
            f"STATEWIDE_PARCELS_WEB_MERCATOR/MapServer/0/query"
        )
        params = {
            "f": "json",
            "where": f"COUNTY_ID = {county_id}",
            "outFields": "OWNER,OWNER2,ADDRESS,CITY,CMAP,GP,PARCEL,GISLINK,DEEDAC,CALCAC,SUBDIV,LOT",
            "returnGeometry": "false",
            "outSR": "4326",
            "resultRecordCount": max_records,
            "token": self._token,
        }

        result = page.evaluate("""
            ([queryUrl, params]) => {
                const url = new URL(queryUrl);
                Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
                return fetch(url.toString()).then(r => r.json());
            }
        """, [query_url, params])

        if result.get("error"):
            logger.warning("ArcGIS query error (county_id=%d): %s", county_id, result["error"].get("message"))
            return []

        features = result.get("features", [])
        return [f.get("attributes", {}) for f in features]

    def _query_parcels_by_address(
        self,
        page,
        county_id: int,
        house_number: str,
        street: str,
    ) -> list[dict]:
        """Query ArcGIS parcels in a county filtered by house number (+ street).

        Filtering by the house number keeps the result set tiny and ensures the
        candidate matching parcel is actually returned (the unfiltered county
        query is capped at 2000 rows by objectid and would miss many parcels).
        """
        query_url = (
            f"{TNMAP_BASE_URL}/arcgis/rest/services/CADASTRAL/"
            f"STATEWIDE_PARCELS_WEB_MERCATOR/MapServer/0/query"
        )
        where = f"COUNTY_ID = {county_id} AND ADDRESS LIKE '%{house_number}%'"
        params = {
            "f": "json",
            "where": where,
            "outFields": "OWNER,OWNER2,ADDRESS,CITY,CMAP,GP,PARCEL,GISLINK,DEEDAC,CALCAC,SUBDIV,LOT",
            "returnGeometry": "false",
            "outSR": "4326",
            "resultRecordCount": "200",
            "token": self._token,
        }

        result = page.evaluate("""
            ([queryUrl, params]) => {
                const url = new URL(queryUrl);
                Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
                return fetch(url.toString()).then(r => r.json());
            }
        """, [query_url, params])

        if result.get("error"):
            logger.warning("ArcGIS address query error (county_id=%d, num=%s): %s",
                           county_id, house_number, result["error"].get("message"))
            return []

        return [f.get("attributes", {}) for f in result.get("features", [])]

    def _get_county_id(self, county_name: str) -> Optional[int]:
        """Look up county ID from alphabetical index."""
        county_lower = county_name.lower().strip()
        try:
            idx = [c.lower() for c in TN_COUNTIES].index(county_lower)
            return idx + 1  # 1-indexed
        except ValueError:
            return None

    def enrich_properties(
        self,
        foreclosure_properties: list[dict],
    ) -> list[dict]:
        """Enrich foreclosure properties with TNMap assessment data.

        Matching strategy:
        1. By county name + address (fuzzy)
        2. By county name + parcel number (exact if parcel available)

        Args:
            foreclosure_properties: List of PropertyData dicts from TNForeclosureScraper

        Returns:
            Same list enriched with tnmap_* fields.
        """
        if not foreclosure_properties:
            return []

        print(f"\n  Enriching {len(foreclosure_properties)} properties with TNMap data")

        with camoufox_context() as page:
            page.set_viewport_size({"width": 1920, "height": 1080})

            try:
                # Get session config and token
                print("  [1/3] Initializing Cartograph session ...", end=" ", flush=True)
                config_data, token = self._get_token(page)
                self._token = token
                print(f"token={token[:20]}...")

                # Build county lookup from config
                counties_from_config = {}
                county_field = config_data.get("sources", {}).get(
                    "assessmentProperties", {}
                ).get("filters", [])
                for f in county_field:
                    if f.get("field") == "COUNTY_ID":
                        for val in f.get("values", {}).get("data", []):
                            counties_from_config[val.get("name", "").lower()] = val.get("id")
                        break

                if counties_from_config:
                    print(f"  Loaded {len(counties_from_config)} counties from config")

                print("  [2/3] Querying parcel data ...", end=" ", flush=True)

                # Match each property individually with an address-targeted
                # parcel query (filtering by house number keeps the result set
                # small and guarantees the matching parcel is returned, since
                # the per-county query is capped at 2000 rows by objectid).
                matched_count = 0
                for prop in foreclosure_properties:
                    county = (prop.get("county") or "").lower().strip()
                    county_id = counties_from_config.get(county)
                    if county_id is None:
                        county_id = self._get_county_id(county)
                    if not county_id:
                        continue

                    num, street = self._normalize_address(prop.get("address") or "")
                    if num is None:
                        continue  # No house number -> cannot match reliably

                    parcels = self._query_parcels_by_address(page, county_id, num, street)
                    enriched = self._match_parcel(prop, parcels, counties_from_config)
                    if enriched:
                        matched_count += 1
                        prop.update(enriched)
                        # Surface TNMap values onto the standard columns so
                        # they persist + render on the dashboard.
                        # Prefer the deeded acreage, but fall back to the
                        # calculated acreage when the deeded value is 0/empty
                        # (TN's CADASTRAL layer frequently stores DEEDAC=0 while
                        # CALCAC holds the real figure).
                        acres = None
                        for _src in (
                            enriched.get("tnmap_deeded_acres"),
                            enriched.get("tnmap_calculated_acres"),
                        ):
                            if _src in (None, "", 0, "0"):
                                continue
                            try:
                                if float(_src) > 0:
                                    acres = float(_src)
                                    break
                            except (TypeError, ValueError):
                                pass
                        if acres is not None:
                            prop["acres"] = acres
                        if enriched.get("tnmap_gislink"):
                            prop["gis_url"] = enriched["tnmap_gislink"]
                        if enriched.get("tnmap_owner"):
                            prop["owner_name"] = enriched["tnmap_owner"]
                        prop["tnmap_data"] = json.dumps(enriched)

                print(f" {matched_count} matched")

                print("  [3/3] Done")

            finally:
                pass

        return foreclosure_properties

    def _match_parcel(
        self,
        prop: dict,
        parcels: list[dict],
        county_names: dict[str, int],
    ) -> Optional[dict]:
        """Match a foreclosure property to a parcel record.

        Only returns a match when a verified key is present: a normalized
        street-address match (house number + street name). County alone is NOT
        sufficient — attaching an arbitrary in-county parcel produces false
        acreage/owner data, so we return None when no address match exists.
        """
        prop_address = (prop.get("address") or "").strip()
        if not prop_address:
            return None

        prop_num, prop_street = self._normalize_address(prop_address)
        if prop_num is None:
            return None

        best_match = None
        for parcel in parcels:
            parcel_address = (parcel.get("ADDRESS") or "").strip()
            if not parcel_address:
                continue
            p_num, p_street = self._normalize_address(parcel_address)
            if p_num != prop_num:
                continue
            if not p_street:
                continue
            # Require the street name to overlap strongly with the property's.
            if self._streets_match(prop_street, p_street):
                best_match = parcel
                break

        if best_match is None:
            return None

        return {
            "tnmap_owner": best_match.get("OWNER"),
            "tnmap_owner2": best_match.get("OWNER2"),
            "tnmap_address": best_match.get("ADDRESS"),
            "tnmap_city": best_match.get("CITY"),
            "tnmap_deeded_acres": best_match.get("DEEDAC"),
            "tnmap_calculated_acres": best_match.get("CALCAC"),
            "tnmap_gislink": best_match.get("GISLINK"),
            "tnmap_cmap": best_match.get("CMAP"),
            "tnmap_gp": best_match.get("GP"),
            "tnmap_parcel": best_match.get("PARCEL"),
            "tnmap_subdivision": best_match.get("SUBDIV"),
        }

    @staticmethod
    def _normalize_address(addr: str) -> tuple[Optional[str], str]:
        """Return (house_number, normalized_street_name) or (None, '') if no number."""
        addr = (addr or "").lower()
        addr = re.sub(r"[^a-z0-9 ]", " ", addr)
        tokens = [t for t in addr.split() if t]
        stopwords = {
            "street", "st", "avenue", "ave", "boulevard", "blvd", "drive", "dr",
            "road", "rd", "lane", "ln", "court", "ct", "circle", "cir", "pkwy",
            "parkway", "hwy", "highway", "pike", "way", "trail", "trl", "place",
            "pl", "north", "south", "east", "west", "n", "s", "e", "w",
        }
        cleaned = [t for t in tokens if t not in stopwords]
        if not cleaned or not cleaned[0].isdigit():
            # No leading house number — fall back to scanning for any number
            for i, t in enumerate(cleaned):
                if t.isdigit():
                    return t, " ".join(cleaned[:i] + cleaned[i + 1:])
            return None, ""
        num = cleaned[0]
        street = " ".join(cleaned[1:])
        return num, street

    @staticmethod
    def _streets_match(street1: str, street2: str) -> bool:
        """Loose street-name equality: token overlap >= 0.5 with a shared token."""
        if not street1 or not street2:
            return False
        if street1 == street2:
            return True
        w1, w2 = set(street1.split()), set(street2.split())
        if not w1 or not w2:
            return False
        common = len(w1 & w2)
        union = len(w1 | w2)
        if common >= 1 and common / union >= 0.5:
            return True
        # Fallback: a directional sometimes fuses to the street name in one
        # source but not the other (e.g. "esevier" vs "e sevier"). Treat the
        # normalized streets as matching when one is a substring of the other.
        s1, s2 = street1.replace(" ", ""), street2.replace(" ", "")
        return bool(s1 and s2) and (s1 in s2 or s2 in s1)

    def _find_chromium(self) -> Optional[str]:
        """Find chromium executable."""
        import glob
        import os
        import shutil
        candidates = [
            "~/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            "~/.cache/ms-playwright/chromium-1226/chrome-linux64/chrome",
            "/opt/opencode/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            shutil.which("chromium"),
        ]
        for c in candidates:
            c = c.strip()
            if "*" in c:
                matches = glob.glob(c)
                if matches:
                    return sorted(matches)[-1]
            elif c and os.path.isfile(c):
                return c
        return None

    def fetch_county_parcels(
        self,
        county_name: str,
        max_records: int = 100,
    ) -> list[dict]:
        """Fetch all parcels for a specific county (useful for enrichment lookup)."""
        county_id = TN_FORECLOSURE_COUNTIES.index(county_name.lower().strip()) + 1

        results = []
        with camoufox_context() as page:
            try:
                # Get token
                config_data, token = self._get_token(page)
                self._token = token

                # Query parcels
                query_url = (
                    f"{TNMAP_BASE_URL}/arcgis/rest/services/CADASTRAL/"
                    f"STATEWIDE_PARCELS_WEB_MERCATOR/MapServer/0/query"
                )
                params = {
                    "f": "json",
                    "where": f"COUNTY_ID = {county_id}",
                    "outFields": "OWNER,OWNER2,ADDRESS,CITY,CMAP,GP,PARCEL,GISLINK,DEEDAC,CALCAC,SUBDIV,LOT",
                    "returnGeometry": "false",
                    "outSR": "4326",
                    "maxRecords": max_records,
                    "token": token,
                }

                result = page.evaluate("""
                    ([queryUrl, params]) => {
                        const url = new URL(queryUrl);
                        Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
                        return fetch(url.toString()).then(r => r.json());
                    }
                """, [query_url, params])

                if result.get("error"):
                    logger.warning("ArcGIS query error: %s", result["error"].get("message"))
                else:
                    for f in result.get("features", []):
                        results.append(f.get("attributes", {}))

            finally:
                pass

        return results


def enrich_with_tnmap(properties: list[dict]) -> list[dict]:
    """Convenience function to enrich foreclosure properties with TNMap data."""
    return TNMapScraper().enrich_properties(properties)
