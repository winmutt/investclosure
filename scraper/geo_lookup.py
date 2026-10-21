"""Town/ZIP -> county cross-reference (imported from the legacy realestate app).

Listings frequently carry only a town name ("Macon, NC 27551"), and Zillow
county-search URLs match *cities* — so Macon (the town, Warren County) leaks
into Macon County searches, and the coordinate guard can't catch rows with
no coordinates. This index (built from the Census ZIP->county table shipped
as realestate/target_counties_zips.csv: 4,917 ZIPs / 3,625 USPS preferred
cities across GA/AL/KY/NC/SC/TN) resolves a listing's town/ZIP to its real
county or counties.

Border towns legitimately span several counties (ZIPs too), and some
town-only lookups are ambiguous; callers keep the searched county unless the
cross-reference explicitly excludes it.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).parent / "town_county.json"


class TownCountyIndex:
    """Town/ZIP -> county lookups over town_county.json."""

    def __init__(self, zips: dict[str, list[str]], cities: dict[str, list[str]]):
        self.zips = zips
        self.cities = cities

    def counties_for_zip(self, zip_code: str, state: str) -> set[str]:
        zp = (zip_code or "").strip()
        if not zp:
            return set()
        return set(self.zips.get(f"{state.upper()}|{zp}", []))

    def counties_for_city(self, city: str, state: str) -> set[str]:
        city = (city or "").strip().lower().replace(".", "").strip()
        if not city:
            return set()
        return set(self.cities.get(f"{state.upper()}|{city}", []))

    def counties(self, state: str, city: str = "",
                 zip_code: str = "") -> set[str]:
        """Counties this listing's town/ZIP can belong to (empty = unknown)."""
        found = self.counties_for_zip(zip_code, state)
        if not found:
            return self.counties_for_city(city, state)
        both = found | self.counties_for_city(city, state)
        return both

    def resolve(self, searched: str, state: str, city: str = "",
                zip_code: str = "",
                targets: Optional[set[str]] = None) -> Optional[str]:
        """Attribute a listing to a county given its searched (URL) county.

        Returns:
          searched  — cross-reference confirms it, is ambiguous between
                      several target counties, or knows nothing (keep as-is)
          other     — cross-reference puts the town/ZIP in exactly ONE
                      target county other than the searched one
          None      — the town/ZIP lies in NO target county in this state
                      (a city-name collision like Macon town -> Warren)
        """
        counties = self.counties(state, city, zip_code)
        searched = (searched or "").lower().replace(" ", "_")
        if not counties:
            return searched
        if searched in counties:
            return searched
        if targets is None:
            targets = set(counties)
        hits = counties & targets
        if not hits:
            logger.info(
                "town/zip %r %r resolves to %s — none are target counties",
                city, zip_code, sorted(counties))
            return None
        if len(hits) == 1:
            resolved = sorted(hits)[0]
            logger.info("town/zip %r %r: reattributing county %s -> %s",
                        city, zip_code, searched, resolved)
            return resolved
        logger.info(
            "town/zip %r %r ambiguous across %s — keeping %s",
            city, zip_code, sorted(hits), searched)
        return searched


def load_index() -> Optional[TownCountyIndex]:
    try:
        raw = json.loads(_DATA_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("town/county index unavailable: %s", exc)
        return None
    return TownCountyIndex(raw.get("zips", {}), raw.get("cities", {}))
