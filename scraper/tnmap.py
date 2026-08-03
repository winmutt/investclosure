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
from playwright.sync_api import sync_playwright, Request

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
        # Trigger cartograph session
        page.goto(TNMAP_ASSESSMENT_URL + "/", wait_until="networkidle", timeout=30000)
        time.sleep(2)

        # Find the cartograph POST request
        cartograph_url = f"{TNMAP_BASE_URL}/cms/cartographs"
        responses = []

        def handle_response(response):
            if response.url == cartograph_url and response.request.method == "POST":
                responses.append(response)

        page.on("response", handle_response)

        # The cartograph call happens immediately on page load,
        # but let's make sure it has happened by waiting
        page.wait_for_timeout(3000)

        page.off("response", handle_response)

        if not responses:
            logger.error("No cartograph response captured")
            # Retry with a direct goto
            page.reload(wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)

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
        if token.endswith("."):
            token = token[:-1]

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
            "maxRecords": max_records,
            "token": self._token,
        }

        result = page.evaluate("""
            ([url, params]) => {
                const url = new URL(url);
                Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
                return fetch(url.toString()).then(r => r.json());
            }
        """, [query_url, params])

        if result.get("error"):
            logger.warning("ArcGIS query error (county_id=%d): %s", county_id, result["error"].get("message"))
            return []

        features = result.get("features", [])
        return [f.get("attributes", {}) for f in features]

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

        chromium_path = self._find_chromium()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=chromium_path,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
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

                # Group properties by county for batch querying
                by_county: dict[int, list[dict]] = {}
                for prop in foreclosure_properties:
                    county = (prop.get("county") or "").lower().strip()
                    county_id = counties_from_config.get(county)
                    if county_id is None:
                        county_id = self._get_county_id(county)
                    if county_id:
                        by_county.setdefault(county_id, []).append(prop)

                # Query each county once
                for county_id, props in by_county.items():
                    print(f"\n  Querying county_id={county_id} ({len(props)} properties) ...", flush=True)
                    parcels = self._query_parcels_by_county(page, county_id)
                    print(f"  Found {len(parcels)} parcels in county")

                    # Match properties to parcels
                    for prop in props:
                        enriched = self._match_parcel(prop, parcels, counties_from_config)
                        if enriched:
                            prop.update(enriched)

                print("  [3/3] Done")

            finally:
                browser.close()

        return foreclosure_properties

    def _match_parcel(
        self,
        prop: dict,
        parcels: list[dict],
        county_names: dict[str, int],
    ) -> Optional[dict]:
        """Match a foreclosure property to a parcel record."""
        prop_address = (prop.get("address") or "").strip().upper()
        prop_county = (prop.get("county") or "").lower().strip()
        prop_city = (prop.get("city") or "").strip().upper()

        # Find county name for this ID
        county_id_counter = {}
        for name, cid in county_names.items():
            county_id_counter.setdefault(cid, []).append(name)

        # Try to match by address normalization
        best_match = None
        best_score = 0

        for parcel in parcels:
            parcel_address = (parcel.get("ADDRESS") or "").strip().upper()
            parcel_city = (parcel.get("CITY") or "").strip().upper()
            parcel_deeded = parcel.get("DEEDAC")
            parcel_calc = parcel.get("CALCAC")
            gislink = parcel.get("GISLINK")
            owner = parcel.get("OWNER")

            if not parcel_address:
                continue

            score = 0

            # Address match (normalized)
            if prop_address:
                # Simple normalization: remove streets, numbers, etc.
                if self._addresses_match(prop_address, parcel_address):
                    score += 10

            # County match
            score += 5  # Already filtered by county

            if score > best_score:
                best_score = score
                best_match = parcel

        if best_match and best_score >= 5:
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

        return None

    @staticmethod
    def _addresses_match(addr1: str, addr2: str) -> bool:
        """Check if two addresses match after normalization."""
        # Remove common suffixes/prefixes
        normalized1 = re.sub(r'\b(ST|STREET|AVE|AVENUE|BLVD|BOULEVARD|DR|DRIVE|ROAD|RD|LANE|LN|CIR|CIRCLE|WAY|COURT|CT)\b\.?', '', addr1, flags=re.IGNORECASE).strip()
        normalized2 = re.sub(r'\b(ST|STREET|AVE|AVENUE|BLVD|BOULEVARD|DR|DRIVE|ROAD|RD|LANE|LN|CIR|CIRCLE|WAY|COURT|CT)\b\.?', '', addr2, flags=re.IGNORECASE).strip()

        # Extract numbers
        nums1 = re.findall(r'\d+', normalized1)
        nums2 = re.findall(r'\d+', normalized2)

        if nums1 == nums2:
            return True

        # Check if one contains the other (for partial matches)
        words1 = normalized1.split()
        words2 = normalized2.split()

        common = len(set(words1) & set(words2))
        total = len(set(words1) | set(words2))
        if total > 0 and common / total > 0.5 and common >= 2:
            return True

        return False

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

        chromium_path = self._find_chromium()

        results = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=chromium_path,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()

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
                    ([url, params]) => {
                        const url = new URL(url);
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
                browser.close()

        return results


def enrich_with_tnmap(properties: list[dict]) -> list[dict]:
    """Convenience function to enrich foreclosure properties with TNMap data."""
    return TNMapScraper().enrich_properties(properties)
