"""Rentals-vertical scope — where a cheap GA house can rent for ~$1,000/mo.

Three committed runtime files answer "which GA counties do we scrape?" and
"what does rent look like there?" (same policy as ``county_radius.json``:
distances and verdicts built by the fetch CLIs, evaluated at runtime):

  scraper/county_radius_atlanta.json — GA county -> nearest-boundary miles
      to downtown Atlanta. Built by
      ``python3 -m scraper.fetch_county_radius --states GA --radius 120
      --origin 33.7540,-84.3905 --origin-label "Downtown Atlanta (Five
      Points)" --out scraper/county_radius_atlanta.json``
  scraper/ga_zips.json  — GA zips whose ZCTA centroid sits within
      RENT_RADIUS_MILES of Atlanta (+ county mapping). Built by
      ``python3 -m scraper.fetch_ga_zips``
  scraper/rent_markets.json — per-zip RentCast market screen verdicts
      (pass/borderline/fail/no_data). Built by
      ``python3 -m scraper.rent_market_scan`` (monthly refresh)

Precedence for the scrape scope (``target_counties()``):

  1. counties holding a ``pass``/``borderline`` zip — the RentCast answer;
  2. no market file yet → every GA county whose NEAREST ground is inside
     RENT_RADIUS_MILES (measured boundary only; an unmeasured county proves
     *out*, never *in* — same rule as the mountains radius table);
  3. neither file → empty scope. The scraper refuses to run rather than
     silently walking all 159 GA counties.
"""
from __future__ import annotations

import json
import logging
import os
import re
from statistics import median
from typing import Any, Dict, List, Optional

from .config import config

logger = logging.getLogger(__name__)

_DIR = os.path.dirname(__file__)
ATL_TABLE_PATH = os.path.join(_DIR, "county_radius_atlanta.json")
ZIPS_PATH = os.path.join(_DIR, "ga_zips.json")
MARKETS_PATH = os.path.join(_DIR, "rent_markets.json")

# A zip counts toward a county's scope at these verdicts.
_SCOPING_VERDICTS = {"pass", "borderline"}


def county_key(name: str) -> str:
    """Match key tolerant of 'van_buren' / 'Van Buren' / 'St. Clair' forms."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.error("cannot read %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Distance half — GA counties near downtown Atlanta
# ---------------------------------------------------------------------------

def in_radius_counties() -> List[str]:
    """GA counties with measured ground within RENT_RADIUS_MILES of Atlanta.

    Counties recorded ``measured: false`` carry only a bounding-box lower
    bound: a bound above the radius proves the county is entirely outside,
    a bound below proves nothing — such counties are excluded here because a
    scrape scope must be affirmatively in, not "maybe".
    """
    table = _load_json(ATL_TABLE_PATH)
    if not table:
        logger.warning("no Atlanta radius table at %s — run "
                       "python3 -m scraper.fetch_county_radius (see "
                       "rent_scope docstring)", ATL_TABLE_PATH)
        return []
    radius = config.RENT_RADIUS_MILES
    if not radius:
        # Rule disabled: no distance opinion, keep every GA county we know.
        return sorted(k.split("/", 1)[1] for k in table.get("counties", {}))
    out = []
    for key, rec in table.get("counties", {}).items():
        state, _, county = key.partition("/")
        if state.upper() != "GA":
            continue
        if rec.get("measured") and rec.get("boundary_mi") is not None \
                and rec["boundary_mi"] <= radius:
            out.append(county)
    return sorted(out)


# ---------------------------------------------------------------------------
# Rentability half — the RentCast zip screen
# ---------------------------------------------------------------------------

def load_markets() -> Dict[str, dict]:
    """zip -> screened market entry ({} when the scan has not run)."""
    data = _load_json(MARKETS_PATH)
    if not data:
        return {}
    return data.get("zips", {}) or {}


def target_counties() -> List[str]:
    """Counties the rentals scraper should walk this run (sorted).

    RentCast-screened counties when the scan has run — intersected with
    the Atlanta radius so a stale markets file cannot drag in far GA
    counties; otherwise the whole in-radius set as the fallback. Empty
    only when both data files are missing (the caller must then refuse to
    scrape, see ``describe_scope``).
    """
    markets = load_markets()
    radius_set = set(in_radius_counties())
    if markets:
        counties = set()
        for entry in markets.values():
            if entry.get("verdict") in _SCOPING_VERDICTS:
                for c in entry.get("counties") or []:
                    counties.add(county_key(c))
        if radius_set:
            counties &= radius_set
        return sorted(counties)
    return sorted(radius_set)


def describe_scope() -> str:
    """One-line human description for run output / logs."""
    markets = load_markets()
    counties = target_counties()
    if not counties:
        return ("NO SCOPE — run python3 -m scraper.fetch_ga_zips, "
                "fetch_county_radius and rent_market_scan first")
    how = ("rentcast screen" if markets
           else "100-mi radius fallback (rentcast screen not run yet)")
    return f"{len(counties)} GA counties via {how}"


def county_rent(county: str) -> Optional[float]:
    """Representative small-home market rent ($/mo) for a row's county.

    Median of the county's screened zips that carry a rent figure,
    qualifying zips preferred; None when nothing is known (the row then
    keeps no rent stamp — absence is never rendered as $0).
    """
    markets = load_markets()
    if not markets or not county:
        return None
    key = county_key(county)
    rents = []
    qualifying_rents = []
    for entry in markets.values():
        counties = {county_key(c) for c in entry.get("counties") or []}
        if key not in counties:
            continue
        rent = entry.get("rent")
        if not rent:
            continue
        rents.append(rent)
        if entry.get("verdict") in _SCOPING_VERDICTS:
            qualifying_rents.append(rent)
    pool = qualifying_rents or rents
    return round(median(pool)) if pool else None
