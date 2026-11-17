"""Distance half of the product qualification rule.

The product sells mountain land within reach of the office: ground at or
above :data:`config.MIN_ELEVATION` (**and** — added 2026-11-17) within
:data:`config.RADIUS_MILES` statute miles of :data:`config.ORIGIN_LAT` /
:data:`config.ORIGIN_LON`. Elevation is measured per parcel by
``scraper/elevation.py``; this module does the distance.

Why a county table is needed at all: most rows have their own coordinates
and are judged directly, but a notice row in a state with no parcel service
(AL, KY, SC) may never get a pin. Rather than keep those on faith, each
county carries its **closest point to the origin** from USGS boundary
geometry (``county_radius.json``, built by
``scraper/fetch_county_radius.py``), so the question "could this row be in
range?" is answered by geometry:

* boundary inside the radius → the county stays in scope; the row is kept
  until a pin says otherwise (``Unicoi, TN`` is the shape of this case: its
  far edge is 170 mi away while its centre is 184)
* boundary outside the radius → the whole county is out, coordinates or not

The table stores distances, not verdicts, so changing
:data:`config.RADIUS_MILES` re-evaluates every county. Counties recorded with
``measured: false`` were only bounded by their bounding box, so they can be
called *outside* while the threshold stays below that bound; above it they
report ``unknown`` and are kept, with a warning to re-run the fetcher.

Nothing here touches the network: the table is a committed file and an
outage must never change what qualifies.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from math import asin, cos, radians, sin, sqrt
from typing import Any, Dict, List, Optional, Tuple

from .config import config

logger = logging.getLogger(__name__)

TABLE_PATH = os.path.join(os.path.dirname(__file__), "county_radius.json")
EARTH_MI = 3958.7613

IN = "in"
OUT = "out"
UNKNOWN = "unknown"

_table_cache: Optional[Dict[str, Any]] = None
_table_cache_path: Optional[str] = None


def county_key(name: str) -> str:
    """Match key tolerant of every county spelling the repo uses.

    ``config.py`` writes ``van_buren``, the scrapers write ``Van Buren``, USGS
    writes ``St. Clair`` — punctuation and spacing are dropped so all three
    reach the same record.
    """
    return re.sub(r"[^a-z]", "", (name or "").lower())


def load_table(path: Optional[str] = None) -> Dict[str, Any]:
    """The USGS county table, cached. Empty dict when the file is missing."""
    global _table_cache, _table_cache_path
    target = path or TABLE_PATH
    if _table_cache is not None and _table_cache_path == target:
        return _table_cache
    try:
        with open(target, encoding="utf-8") as fh:
            _table_cache = json.load(fh)
    except FileNotFoundError:
        logger.warning("county radius table missing (%s) — run "
                       "python3 -m scraper.fetch_county_radius", target)
        _table_cache = {"counties": {}}
    except Exception as e:
        logger.error("county radius table unreadable (%s): %s", target, e)
        _table_cache = {"counties": {}}
    _table_cache_path = target
    return _table_cache


def county_record(state: str, county: str,
                  path: Optional[str] = None) -> Optional[dict]:
    """The USGS row for a county, or None if the table does not know it."""
    if not state or not county:
        return None
    key = f"{state.strip().upper()}/{county_key(county)}"
    return load_table(path).get("counties", {}).get(key)


def enabled() -> bool:
    """False when the rule is switched off (RADIUS_MILES <= 0)."""
    return (config.RADIUS_MILES or 0.0) > 0.0


def miles_from_origin(lat: Optional[float],
                      lon: Optional[float]) -> Optional[float]:
    """Great-circle distance from the origin, statute miles; None if no pin."""
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    origin = (config.ORIGIN_LAT, config.ORIGIN_LON)
    p1, l1 = radians(origin[0]), radians(origin[1])
    p2, l2 = radians(lat_f), radians(lon_f)
    h = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin((l2 - l1) / 2) ** 2
    return 2 * EARTH_MI * asin(sqrt(h))


def county_verdict(state: str, county: str,
                   radius: Optional[float] = None,
                   path: Optional[str] = None) -> Tuple[str, Optional[float]]:
    """``(verdict, miles)`` for a county as a whole.

    ``miles`` is the county's closest point to the origin when geometry gave
    it, otherwise the bounding-box lower bound — enough to prove "outside",
    never enough to prove "inside".
    """
    if not enabled():
        return IN, None
    rec = county_record(state, county, path=path)
    if not rec:
        return UNKNOWN, None
    limit = config.RADIUS_MILES if radius is None else radius
    dist = rec.get("boundary_mi")
    if dist is None:
        return UNKNOWN, None
    if dist <= limit:
        # Only a measured distance can qualify a county.
        return (IN if rec.get("measured", True) else UNKNOWN), dist
    return OUT, dist


def parcel_verdict(lat: Optional[float], lon: Optional[float],
                   state: str = "", county: str = "",
                   radius: Optional[float] = None,
                   path: Optional[str] = None) -> Tuple[str, Optional[float]]:
    """``(verdict, miles)`` for one property: its own pin if it has one, else
    the judgement its county can support."""
    if not enabled():
        return IN, None
    limit = config.RADIUS_MILES if radius is None else radius
    dist = miles_from_origin(lat, lon)
    if dist is not None:
        return (IN if dist <= limit else OUT), dist
    return county_verdict(state, county, radius=limit, path=path)


# ---------------------------------------------------------------------------
# Applying the rule to the database
# ---------------------------------------------------------------------------

def reason_for(verdict_basis: str, miles: Optional[float],
               radius: Optional[float] = None) -> str:
    """Archive reason, kept in the ``outside_radius`` family so one LIKE
    reverses the whole reap the way ``below_min_elevation`` does."""
    limit = config.RADIUS_MILES if radius is None else radius
    label = f"outside_radius ({limit:g} mi)"
    if verdict_basis == "county":
        return f"{label} — county entirely outside"
    return label


def find_outside(conn: sqlite3.Connection, status: str = "active",
                 source: Optional[str] = None,
                 min_id: Optional[int] = None) -> Tuple[List[dict], int]:
    """Rows that fail the distance rule, plus a count of undecidable rows.

    Each hit is ``{id, basis, miles, county, state}`` where ``basis`` is
    ``parcel`` (measured from its own pin) or ``county`` (no pin, and the
    entire county is outside).
    """
    sql = ("SELECT id, state, county, latitude, longitude FROM properties "
           "WHERE status = ?")
    params: list[Any] = [status]
    if source:
        sql += " AND source = ?"
        params.append(source)
    if min_id is not None:
        sql += " AND id > ?"
        params.append(min_id)

    hits: List[dict] = []
    unknown = 0
    for rid, state, county, lat, lon in conn.execute(sql, params).fetchall():
        dist = miles_from_origin(lat, lon)
        if dist is not None:
            if dist > config.RADIUS_MILES:
                hits.append({"id": rid, "basis": "parcel",
                             "miles": round(dist, 1), "county": county,
                             "state": state})
            continue
        verdict, mi = county_verdict(state or "", county or "")
        if verdict == OUT:
            hits.append({"id": rid, "basis": "county",
                         "miles": round(mi, 1) if mi else None,
                         "county": county, "state": state})
        elif verdict == UNKNOWN:
            unknown += 1
    return hits, unknown


def apply_radius_rule(conn: sqlite3.Connection, archive: bool = True,
                      status: str = "active", source: Optional[str] = None,
                      min_id: Optional[int] = None) -> dict:
    """Archive (or just report) the rows outside the radius.

    Archiving goes through :func:`db.archive_property`, so every row gets its
    audit entry and shows in the dashboard History — the same shape as the
    elevation and acreage reaps, and equally reversible:

        UPDATE properties SET status='active', archived_at=NULL,
                              archive_reason=NULL
        WHERE archive_reason LIKE 'outside_radius%'
    """
    stats = {"checked": True, "outside": 0, "archived": 0, "unknown": 0,
             "radius_mi": config.RADIUS_MILES}
    if not enabled():
        stats["checked"] = False
        logger.info("distance rule disabled (RADIUS_MILES=%s)",
                    config.RADIUS_MILES)
        return stats

    hits, unknown = find_outside(conn, status=status, source=source,
                                 min_id=min_id)
    stats.update(outside=len(hits), unknown=unknown)
    if not hits:
        if unknown:
            logger.info("distance rule: %d row(s) undecidable (no USGS "
                        "county match) — kept", unknown)
        return stats

    by_county_basis = sum(1 for h in hits if h["basis"] == "county")
    logger.info("distance rule: %d row(s) outside %g mi (%d by county, "
                "no pin); %d undecidable kept", len(hits),
                config.RADIUS_MILES, by_county_basis, unknown)
    if not archive:
        return stats

    from .db import archive_property
    for hit in hits:
        if archive_property(conn, hit["id"], reason_for(hit["basis"],
                                                        hit["miles"]),
                            archived_by="system"):
            stats["archived"] += 1
    conn.commit()
    return stats
