"""Build ``scraper/ga_zips.json`` — GA zips within the rentals radius.

The universe is the Census-derived ZIP→county cross-reference already
shipped as ``scraper/town_county.json`` (``zips`` key: ``"GA|30115" ->
["bartow", …]``). Centroids come from the Census **Gazetteer Files**
(ZCTA national file, tab-separated, INTPTLON/INTPTLAT centroid per ZCTA):

  https://www2.census.gov/geo/docs/maps-data/data/gazetteer/
      <YEAR>_Gazetteer/<YEAR>_Gaz_zcta_national.zip

Each zip's centroid is measured (haversine) against
``config.RENT_ORIGIN_LAT/LON`` (downtown Atlanta) and kept when inside
``config.RENT_RADIUS_MILES``. Zips present in town_county but absent from
the gazetteer (PO-box-only / non-unique ZIPs) are reported and dropped —
no centroid means no distance verdict.

Downloaded through camoufox (project fetch policy). Rebuild whenever the
rentals origin/radius changes::

    python3 -m scraper.fetch_ga_zips
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import time
import zipfile
from datetime import date
from typing import Dict, List, Optional, Tuple

from .base import camoufox_context
from .config import config
from .fetch_county_radius import _haversine_mi

logger = logging.getLogger(__name__)

OUT_PATH = os.path.join(os.path.dirname(__file__), "ga_zips.json")
TOWN_COUNTY_PATH = os.path.join(os.path.dirname(__file__),
                                "town_county.json")
REQUEST_TIMEOUT_MS = 180000

# Newest first — any year that answers works; the join is by zip, and
# ZCTA centroids barely move between vintages.
GAZETTEER_URLS = [
    ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
     "{y}_Gazetteer/{y}_Gaz_zcta_national.zip").format(y=y)
    for y in (2026, 2025, 2024, 2023, 2022, 2020)
]


def _ga_zip_counties() -> Dict[str, List[str]]:
    """zip -> county list from the shipped ZIP→county cross-reference."""
    with open(TOWN_COUNTY_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    out: Dict[str, List[str]] = {}
    for key, counties in (data.get("zips") or {}).items():
        state, _, zip_code = key.partition("|")
        if state.upper() == "GA" and zip_code:
            out[zip_code] = counties
    return out


def _find_columns(fieldnames: List[str]) -> Tuple[str, str, str]:
    """Locate zip/lon/lat columns across gazetteer vintages.

    Vintage differences observed live: the zip column is ZCTA5CE10 …
    ZCTA5CE20 on older files and bare GEOID on the 2020s files; longitude
    is INTPTLON10/20 vs INTPTLONG. Match on prefixes, oldest spelling
    first so a ZCTA5* column always wins when present.
    """

    def pick(*prefixes: str) -> str:
        for name in fieldnames:
            bare = name.strip()
            if any(bare.upper().startswith(p) for p in prefixes):
                return name
        raise RuntimeError(
            f"gazetteer columns {fieldnames} lack one of {prefixes}")

    return pick("ZCTA5", "GEOID"), pick("INTPTLON", "INTPTLONG"), pick("INTPTLAT")


def download_centroids(page) -> Dict[str, Tuple[float, float]]:
    """zip -> (lat, lon) from the first gazetteer vintage that answers."""
    last_err: Optional[Exception] = None
    for url in GAZETTEER_URLS:
        try:
            resp = page.request.get(url, timeout=REQUEST_TIMEOUT_MS)
            if resp.status != 200:
                logger.warning("gazetteer %s -> HTTP %s", url, resp.status)
                continue
            raw = resp.body()
            zf = zipfile.ZipFile(io.BytesIO(raw))
            name = next(n for n in zf.namelist()
                        if n.lower().endswith((".txt", ".csv")))
            text = zf.read(name).decode("latin-1")
            reader = csv.DictReader(io.StringIO(text), delimiter="\t")
            zcol, loncol, latcol = _find_columns(reader.fieldnames or [])
            out: Dict[str, Tuple[float, float]] = {}
            for row in reader:
                zip_code = (row.get(zcol) or "").strip()
                try:
                    lat = float(row[latcol])
                    lon = float(row[loncol])
                except (TypeError, ValueError, KeyError):
                    continue
                if zip_code:
                    out[zip_code] = (lat, lon)
            logger.info("gazetteer %s: %d ZCTA centroids",
                        url.rsplit("/", 1)[-1], len(out))
            return out
        except Exception as e:  # noqa: BLE001 — try the next vintage
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("gazetteer %s failed: %s", url, last_err)
            time.sleep(1.0)
    raise RuntimeError(f"no gazetteer vintage fetched; last error: {last_err}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build scraper/ga_zips.json (GA zips near Atlanta).")
    parser.add_argument("--radius", type=float,
                        default=config.RENT_RADIUS_MILES or 100.0)
    parser.add_argument("--out", default=OUT_PATH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: "
                               "%(message)s")
    zip_counties = _ga_zip_counties()
    logger.info("universe: %d GA zips from town_county.json", len(zip_counties))

    origin = (config.RENT_ORIGIN_LAT, config.RENT_ORIGIN_LON)
    with camoufox_context() as page:
        centroids = download_centroids(page)

    kept: Dict[str, dict] = {}
    missing = 0
    for zip_code, counties in sorted(zip_counties.items()):
        c = centroids.get(zip_code)
        if c is None:
            missing += 1
            continue
        miles = _haversine_mi(c[0], c[1], (origin[1], origin[0]))
        if miles <= args.radius:
            kept[zip_code] = {
                "counties": counties,
                "lat": c[0], "lon": c[1],
                "mi_atl": round(miles, 1),
            }
    payload = {
        "generated": date.today().isoformat(),
        "source": ("Census Gazetteer ZCTA national file (centroid) joined "
                   "with town_county.json ZIP->county"),
        "origin": {"lat": origin[0], "lon": origin[1],
                   "label": "Downtown Atlanta (Five Points)"},
        "radius_mi": args.radius,
        "zips": kept,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    print(f"wrote {args.out}: {len(kept)} GA zips within {args.radius:g} mi "
          f"of downtown Atlanta ({missing} universe zips lack a gazetteer "
          f"centroid)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
