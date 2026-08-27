"""Kania Law Firm - NC Tax Foreclosure Listings scraper.

Scrapes the Kania Law Firm tax foreclosure table hosted on
kanialawfirm.com which uses WordPress Ninja Tables with an AJAX backend.

The API returns ~180 NC property records with fields:
  county, address, parcel, saledatetime, openingbid, currentbid,
  closedate, propertytype, courtfile, ourfile

Properties are enriched with GIS data using NC OneMap statewide service:
  - All 100 NC counties supported via single Feature Service
  - Acreage from standardized 'gisacres' field (not per-county hacks)
  - Owner name, land use from parcel record
  - Address-based fallback via ESRI World Geocoder
  - Properties below MIN_ACRES with real GIS data saved for accuracy
  - Commercial properties excluded via code filter
"""
from __future__ import annotations
import json
import logging
import re
import time
import random
from typing import Optional, Any

from .base import BaseScraper, PropertyData, camoufox_context, CamoufoxFetcher
from .config import config, QUALIFYING_COUNTIES
from .nc_gis_lookup import build_gis_url, build_google_maps_url, build_google_maps_topo_url

logger = logging.getLogger(__name__)


CANIA_API_URL = (
    "https://kanialawfirm.com/wp-admin/admin-ajax.php"
    "?action=wp_ajax_ninja_tables_public_action"
    "&table_id=216745"
    "&target_action=get-all-data"
    "&skip_rows=0"
    "&limit_rows=0"
    "&default_sorting=old_first"
)


class KaniaLawScraper(BaseScraper):
    SOURCE_NAME = "kania_law"
    MIN_ACRES = config.MIN_ACRES

    def __init__(self, delay_range: tuple[float, float] = (0.5, 1.5)):
        super().__init__(delay_range=delay_range, use_selenium=False)

    def scrape(self) -> list[PropertyData]:
        """Fetch all records from the Kania law firm API in a single call, filtered to qualifying NC counties."""
        # NC mountain counties only (from qualifying_counties.json NON_QUALIFYING list inverted)
        NC_QUALIFYING = {
            "alleghany", "ashe", "avery", "buncombe", "burke", "cherokee",
            "clay", "graham", "haywood", "henderson", "jackson", "macon", "madison",
            "mcdowell", "mitchell", "polk", "swain", "transylvania", "watauga", "yancey",
        }

        logger.info("Fetching Kania Law tax foreclosure listings ...")

        try:
            with camoufox_context() as page:
                fetcher = CamoufoxFetcher(page)
                fetcher.set_headers({
                    "Accept": "application/json",
                    "Referer": "https://kanialawfirm.com/tax-foreclosures/foreclosure-listings/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/131.0.0.0 Safari/537.36",
                })
                raw = fetcher.get(CANIA_API_URL, timeout=60000)

            data = json.loads(raw) if raw else None
            if not isinstance(data, list):
                logger.error("Expected JSON array, got %s", type(data).__name__)
                return []

            logger.info("Got %d raw records from Kania API", len(data))
            all_properties: list[PropertyData] = []
            skipped = 0

            for record in data:
                if not isinstance(record, dict):
                    continue
                val = record.get("value")
                if not isinstance(val, dict):
                    continue
                # Check county first - skip if not in qualifying NC counties
                county = (val.get("county") or "").strip().lower()
                if not county or county not in NC_QUALIFYING:
                    skipped += 1
                    continue
                prop = self._parse_record(val)
                if prop:
                    all_properties.append(prop)
                # Rate limit between records (avoids NC1Map overload during enrichment)
                self._random_delay()

            if skipped:
                logger.info("Skipped %d records from non-qualifying counties", skipped)

            return all_properties

        except Exception as e:
            logger.error("KaniaLaw fetch failed: %s", e, exc_info=True)
            return []

    # ------------------------------------------------------------------
    _HTML_STRIP = re.compile(r"<[^>]+>")

    @staticmethod
    def _clean_html(text: str) -> str:
        return re.sub(KaniaLawScraper._HTML_STRIP, " ", text).strip()

    @staticmethod
    def _price_to_cents(text: str) -> int:
        if not text:
            return 0
        clean = re.sub(r"[^\d.]", "", text)
        try:
            return int(float(clean) * 100)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _clean_date(text: str) -> str:
        if not text:
            return ""
        return KaniaLawScraper._clean_html(text).strip()

    def _parse_record(self, rec: dict[str, Any]) -> Optional[PropertyData]:
        """Parse a single Ninja Tables row into PropertyData."""
        county = (rec.get("county") or "").strip()
        property_type = (rec.get("propertytype") or "").strip().lower()
        if not county:
            return None

        # Skip commercial properties
        if "commercial" in property_type:
            return None

        # --- price (openingbid, fallback to currentbid) ---
        bid_text = rec.get("openingbid") or rec.get("currentbid") or ""
        price_cents = self._price_to_cents(bid_text)

        # --- address / city ---
        raw_address = (rec.get("address") or "").strip()
        address = self._clean_html(raw_address).strip() or None
        city = None
        if address:
            parts = [p.strip() for p in re.split(r",\s*", address) if p.strip()]
            if len(parts) >= 2:
                city = parts[-1]
                address = ", ".join(parts[:-1])

        state = "NC"  # Kania Law only covers NC

        # --- description (combine all fields) ---
        desc_parts = []
        saledatetime = self._clean_date(rec.get("saledatetime", ""))
        closedate = self._clean_date(rec.get("closedate", ""))
        currentbid = (rec.get("currentbid") or "").strip()
        parcel = self._clean_html(rec.get("parcel") or "").strip()
        courtfile = (rec.get("courtfile") or "").strip()

        if saledatetime and "not yet set" not in saledatetime.lower():
            desc_parts.append(f"Sale: {saledatetime}")
        if currentbid:
            desc_parts.append(f"Upset bid: {currentbid}")
        if closedate:
            desc_parts.append(f"Close date: {closedate}")
        if parcel:
            desc_parts.append(f"Parcel: {parcel}")
        if courtfile:
            desc_parts.append(f"Court file: {courtfile}")
        if property_type:
            desc_parts.append(f"Type: {rec['propertytype']}")

        description = " | ".join(desc_parts) if desc_parts else None

        # Kania doesn't publish acres — start with None, let GIS enrichment resolve it.
        acres = None

        # Foreclosure-specific fields for change detection
        saledatetime_raw = (rec.get("saledatetime") or "").strip()
        closedate_raw = (rec.get("closedate") or "").strip()
        currentbid_raw = (rec.get("currentbid") or "").strip()
        openingbid_raw = rec.get("openingbid") or ""

        prop: PropertyData = {
            "source": self.SOURCE_NAME,
            "source_listing_id": rec.get("courtfile") or str(rec.get("___id___", "")),
            "court_case": rec.get("courtfile") or None,
            "url": "https://kanialawfirm.com/tax-foreclosures/foreclosure-listings/",
            "address": address,
            "city": city,
            "county": county or None,
            "state": state,
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": price_cents or None,
            "acres": acres,
            "acres_source": "placeholder",
            "description": description,
            "property_type": property_type or None,
            "image_url": None,
            "parcel_number": parcel or None,
            "gis_url": build_gis_url(None, None, parcel or None, address, county) if (parcel or address) else None,
            "auction_date": saledatetime_raw or None,
            "close_date": closedate_raw or None,
            "upset_bid": currentbid_raw or None,
            "foreclosure_key": "|".join([saledatetime_raw, closedate_raw, currentbid_raw, openingbid_raw]),
            "google_maps_url": build_google_maps_url(None, None, address, city, county) if address else None,
            "google_maps_topo_url": build_google_maps_topo_url(None, None, address, city, county) if address else None,
        }

        return prop
