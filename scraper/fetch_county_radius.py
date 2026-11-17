"""Build ``scraper/county_radius.json`` from USGS county boundaries.

The product rule is "ground at or above ``config.MIN_ELEVATION`` **and**
within ``config.RADIUS_MILES`` of the origin". Elevation is measured per
parcel (``scraper/elevation.py``); this supplies the distance half for the
cases a parcel pin cannot answer — rows with no coordinates at all, and the
"should this county still be scraped at all" question.

Source: USGS The National Map **Governmental Unit Boundaries**
``carto.nationalmap.gov/arcgis/rest/services/govunits/MapServer`` layer 23
("County or Equivalent", polygons) — the federal county boundary layer.

Which USGS hosts are usable from this network (measured 2026-11-17, host and
container agree): ``carto.``/``elevation.``/``hydro.``/``epqs.nationalmap.gov``
answer; ``services.nationalmap.gov`` and ``edc.usgs.gov`` have **no address
records** (DNS answers NOERROR with zero A/AAAA, Firefox reports ENOTFOUND),
so they are not reachable from anywhere, not merely from the container.
``geonames.usgs.gov`` does resolve and answers 200, but the GNIS service is
retired: every path redirects to ``usgs.gov/us-board-on-geographic-names``,
so it publishes no data. Boundary geometry — not a gazetteer — is also what
the rule needs, because the test is the county's NEAREST point to the origin.

What is stored per county:

  boundary_mi   closest point of the county boundary to the origin (0.0 when
                the origin is inside the county)
  centroid_mi   area-weighted centre of the county
  measured      true when the boundary distance was computed from geometry;
                false when the county's bounding box was already farther than
                the search radius, in which case boundary_mi is a lower bound

The radius itself is NOT baked in: ``scraper/radius.py`` compares these
distances against ``config.RADIUS_MILES`` at runtime, so raising or lowering
the threshold works from the same file. A county with ``measured: false`` can
only be called outside while the threshold stays below its ``boundary_mi``;
``radius.py`` reports those as unknown rather than guessing, and you rerun
this fetcher with ``--radius`` set to the new threshold.

Geometry notes: shapely is not installed, so containment is ray casting and
the boundary distance is a minimum over segments densified to ~0.01 deg
(~0.7 mi); polygons come back simplified at ``maxAllowableOffset`` 0.002 deg
(~0.14 mi). A boundary distance is therefore good to roughly a mile.

Usage::

    python3 -m scraper.fetch_county_radius                # six product states
    python3 -m scraper.fetch_county_radius --radius 220   # wider search
    python3 -m scraper.fetch_county_radius --states NC,TN --out /tmp/t.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import date
from math import asin, cos, radians, sin, sqrt
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from .base import camoufox_context
from .config import config

logger = logging.getLogger(__name__)

BASE = ("https://carto.nationalmap.gov/arcgis/rest/services/govunits/"
        "MapServer/23")
TABLE_PATH = os.path.join(os.path.dirname(__file__), "county_radius.json")

# 1690 Little Willeo Rd, Marietta GA 30068 — Census geocoder, exact match.
ORIGIN_LABEL = "1690 Little Willeo Rd, Marietta GA 30068"
EARTH_MI = 3958.7613
STEP_DEG = 0.01
MAX_OFFSET_DEG = 0.002
REQUEST_DELAY = 0.8
REQUEST_TIMEOUT_MS = 180000

# Census state FIPS for the six product states.
STATE_FIPS = {"AL": "01", "GA": "13", "KY": "21", "NC": "37", "SC": "45",
              "TN": "47"}


# ---------------------------------------------------------------------------
# Geometry (no shapely in the image)
# ---------------------------------------------------------------------------

def county_key(name: str) -> str:
    """Match key for a county name, tolerant of every form the repo uses.

    ``config.py`` says ``van_buren``, the scrapers write ``Van Buren``, USGS
    says ``St. Clair``; all three must find the same row, so punctuation and
    spacing are dropped entirely.
    """
    import re
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _haversine_mi(lat: float, lon: float, origin: Tuple[float, float]) -> float:
    p1, l1 = radians(origin[1]), radians(origin[0])
    p2, l2 = radians(lat), radians(lon)
    h = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin((l2 - l1) / 2) ** 2
    return 2 * EARTH_MI * asin(sqrt(h))


def densify(ring: Sequence[Tuple[float, float]],
            step: float = STEP_DEG) -> List[Tuple[float, float]]:
    """Insert points along each edge so no gap exceeds *step* degrees."""
    out: List[Tuple[float, float]] = []
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        out.append((x1, y1))
        n = int(max(abs(x2 - x1), abs(y2 - y1)) / step)
        for i in range(1, n):
            frac = i / n
            out.append((x1 + (x2 - x1) * frac, y1 + (y2 - y1) * frac))
    if ring:
        out.append(ring[-1])
    return out


def bbox_of(rings: Iterable[Sequence[Tuple[float, float]]]
            ) -> Optional[Tuple[float, float, float, float]]:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def bbox_lower_bound_mi(box: Tuple[float, float, float, float],
                        origin: Tuple[float, float]) -> float:
    """Distance from the origin to the nearest point of a lon/lat box.

    Admissible: the true boundary distance can never be smaller, which is
    what lets a county whose box is already outside the search circle skip
    the expensive measurement.
    """
    xmin, ymin, xmax, ymax = box
    lon = min(max(origin[0], xmin), xmax)
    lat = min(max(origin[1], ymin), ymax)
    return _haversine_mi(lat, lon, origin)


def origin_inside(rings: Iterable[Sequence[Tuple[float, float]]],
                  origin: Tuple[float, float]) -> bool:
    """Ray casting on the lon/lat plane (sound for county-sized polygons)."""
    x, y = origin
    inside = False
    for ring in rings:
        pts = densify(ring, 0.05)
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if (y1 > y) != (y2 > y):
                if x < x1 + (y - y1) / (y2 - y1) * (x2 - x1):
                    inside = not inside
    return inside


def polygon_centroid(rings: Iterable[Sequence[Tuple[float, float]]]
                     ) -> Optional[Tuple[float, float]]:
    """Area-weighted centre; interior rings subtract area."""
    area = cx = cy = 0.0
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            cross = x1 * y2 - x2 * y1
            area += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
    if abs(area) < 1e-12:
        return None
    area /= 2.0
    return cx / (6 * area), cy / (6 * area)


def boundary_miles(rings: Iterable[Sequence[Tuple[float, float]]],
                   origin: Tuple[float, float]) -> Optional[float]:
    """Closest distance from the origin to any point on the boundary."""
    best: Optional[float] = None
    for ring in rings:
        for lon, lat in densify(ring):
            d = _haversine_mi(lat, lon, origin)
            if best is None or d < best:
                best = d
    return best


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

def _get_json(page, url: str) -> dict:
    resp = page.request.get(url, timeout=REQUEST_TIMEOUT_MS)
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status} from {url[:110]}")
    data = json.loads(resp.text())
    if "error" in data:
        raise RuntimeError(f"USGS service error: {data['error']}")
    return data


def _attrs(features: Iterable[dict]):
    """Attributes with normalised keys: the service returns them lower case
    and only reports upper case in fieldAliases."""
    for feat in features:
        yield {str(k).lower(): v for k, v in (feat.get("attributes") or {})
               .items()}


def fetch_state(page, fips: str, origin: Tuple[float, float],
                radius_mi: float) -> Dict[str, dict]:
    """All counties of one state, with distance to the origin."""
    where = quote(f"STATE_FIPSCODE = '{fips}'")
    url = (f"{BASE}/query?where={where}&outFields=*&returnGeometry=true"
           f"&outSR=4326&maxAllowableOffset={MAX_OFFSET_DEG}"
           f"&resultRecordCount=2000&f=json")
    feats = _get_json(page, url).get("features") or []
    out: Dict[str, dict] = {}
    exact = skipped = 0
    for feat, a in zip(feats, _attrs(feats)):
        rings = [[tuple(p[:2]) for p in r]
                 for r in (feat.get("geometry") or {}).get("rings") or []]
        if not rings:
            continue
        box = bbox_of(rings)
        if box is None:
            continue
        centre = polygon_centroid(rings)
        lower = bbox_lower_bound_mi(box, origin)
        if lower > radius_mi:
            # The circle is inside its own bounding box, so a county whose
            # box misses that box cannot contain a point inside the radius.
            dist, measured = lower, False
            skipped += 1
        else:
            dist = (0.0 if origin_inside(rings, origin)
                    else boundary_miles(rings, origin) or lower)
            measured = True
            exact += 1
        name = (a.get("county_name") or "").strip()
        if not name:
            continue
        out[f"{county_key(name)}"] = {
            "county": name.title(),
            "fips": a.get("stco_fipscode"),
            "gnis_id": a.get("gnis_id"),
            "boundary_mi": round(dist, 1),
            "centroid_mi": (round(_haversine_mi(centre[1], centre[0], origin), 1)
                            if centre else None),
            "measured": measured,
        }
    logger.info("%s: %d counties (%d measured, %d outside the search box)",
                next((s for s, f in STATE_FIPS.items() if f == fips), fips),
                len(out), exact, skipped)
    return out


def build_table(states: Sequence[str], origin: Tuple[float, float],
                radius_mi: float) -> dict:
    table: Dict[str, dict] = {}
    with camoufox_context() as page:
        for st in states:
            fips = STATE_FIPS.get(st.upper())
            if not fips:
                logger.warning("no state FIPS for %s — skipped", st)
                continue
            for key, row in fetch_state(page, fips, origin,
                                        radius_mi).items():
                table[f"{st.upper()}/{key}"] = row
            time.sleep(REQUEST_DELAY)
    return {
        "generated": date.today().isoformat(),
        "source": ("USGS The National Map, Governmental Unit Boundaries "
                   "(carto.nationalmap.gov govunits layer 23, county "
                   "polygons); simplified to 0.002 deg"),
        "origin": {"lat": origin[1], "lon": origin[0], "label": ORIGIN_LABEL},
        "search_radius_mi": radius_mi,
        "counties": table,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the USGS county-distance table used by the "
                    "distance qualification rule.")
    parser.add_argument("--states", default=",".join(STATE_FIPS),
                        help="comma-separated state abbreviations "
                             f"(default: {','.join(STATE_FIPS)})")
    parser.add_argument("--radius", type=float,
                        default=config.RADIUS_MILES or 180.0,
                        help="search radius in miles (default: the "
                             "configured RADIUS_MILES, else 180)")
    parser.add_argument("--out", default=TABLE_PATH,
                        help=f"output file (default: {TABLE_PATH})")
    args = parser.parse_args()

    origin = (config.ORIGIN_LON, config.ORIGIN_LAT)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: "
                               "%(message)s")
    table = build_table([s.strip().upper() for s in args.states.split(",")
                         if s.strip()], origin, args.radius)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=1, sort_keys=True)
    counties = table["counties"]
    inside = sum(1 for c in counties.values()
                 if c["measured"] and c["boundary_mi"] <= args.radius)
    print(f"wrote {args.out}: {len(counties)} counties, {inside} within "
          f"{args.radius:g} mi of {ORIGIN_LABEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
