"""Per-parcel ground elevation from USGS 3DEP (public, key-free).

Why this exists: the qualifying county lists only encode each county's
**peak** elevation (>=1700ft, `config.py`), which says nothing about a
parcel sitting in a valley — 70% of a live sample of active rows measured
below 1,700ft (all SC/AL, most GA/TN foothills). The legacy realestate app
had a per-property `MIN_ELEVATION` filter backed by the Google Maps
Elevation API; that was not ported. This module restores the measurement
with USGS 3DEP instead of Google (no API key, all six states, 1 m coverage).

Two endpoints, both public ArcGIS services:

* ``3DEPElevation/ImageServer/getSamples`` — up to 10 points per request
  (measured 2026-11-09: 10 fine, 20 answers 502). Returns **metres**.
* ``epqs.nationalmap.gov/v1/json`` — one point per request, returns
  **feet** directly; used when a batch keeps failing.

Plain HTTP (curl_cffi) is the deliberate choice here, exactly as in
``nc_gis_lookup``/``nconemap_net``: these are read-only public raster
services with no browser requirements, and the enrichment walks thousands of
points — driving one Camoufox page per request would make it unworkable.
Requests are paced (>=2/s project-wide budget) and retried with the batch
split in half, so one flaky response cannot stall a pass.

:func:`enrich_elevations` writes ``properties.elevation_ft`` and, when
``archive`` is set, reaps active rows below :data:`config.MIN_ELEVATION` via
:func:`db.archive_below_elevation` (audited, reason ``below_min_elevation``).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Iterable, Optional, Sequence

from .config import config
from .db import _ensure_db, archive_below_elevation

logger = logging.getLogger(__name__)

SAMPLES_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
               "3DEPElevation/ImageServer/getSamples")
EPQS_URL = ("https://epqs.nationalmap.gov/v1/json?x={lon}&y={lat}"
            "&units=Feet&wkid=4326&includeDate=false")

# Measured limit: 10 points answer 200, 20 answer 502 (2026-11-09).
MAX_POINTS_PER_REQUEST = 10
WRITE_CHUNK = 100
M_PER_FT = 0.3048
REQUEST_DELAY = (0.25, 0.75)
REQUEST_TIMEOUT = 60


_local = threading.local()


def _session():
    """Thread-local curl_cffi session (browser TLS fingerprint, keep-alive).

    One session per thread so thousands of samples reuse connections instead
    of opening a fresh TLS handshake per point.
    """
    session = getattr(_local, "session", None)
    if session is None:
        from curl_cffi import requests as curl_requests
        session = curl_requests.Session(impersonate="chrome131")
        _local.session = session
    return session


def _sleep() -> None:
    time.sleep(random.uniform(*REQUEST_DELAY))


def _get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET a JSON document, or None on any failure."""
    try:
        session = _session()
        resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.debug("elevation HTTP %s for %s", resp.status_code, url)
            return None
        return resp.json()
    except Exception as exc:            # network, JSON, TLS — all non-fatal
        logger.debug("elevation request failed (%s): %s", url, exc)
        return None


def _samples_batch(points: Sequence[tuple[float, float]]
                   ) -> list[Optional[float]]:
    """Elevations (feet) for up to MAX_POINTS_PER_REQUEST (lat, lon) pairs."""
    encoded = ",".join(f"[{lon},{lat}]" for lat, lon in points)
    params = {
        "geometry": ('{"points":[%s],"spatialReference":{"wkid":4326}}'
                     % encoded),
        "geometryType": "esriGeometryMultipoint",
        "f": "json",
    }
    data = _get_json(SAMPLES_URL, params)
    samples = (data or {}).get("samples") or []
    out: list[Optional[float]] = []
    for sample in samples[:len(points)]:
        try:
            out.append(float(sample["value"]) / M_PER_FT)
        except (KeyError, TypeError, ValueError):
            out.append(None)           # "NoData"
    out += [None] * (len(points) - len(out))
    return out


def _epqs_point(lat: float, lon: float) -> Optional[float]:
    """Single-point fallback; EPQS already returns feet."""
    data = _get_json(EPQS_URL.format(lat=lat, lon=lon))
    try:
        return float((data or {}).get("value"))
    except (TypeError, ValueError):
        return None


def elevation_ft(lat: float, lon: float) -> Optional[float]:
    """Ground elevation in feet for one coordinate, or None."""
    values = elevations([(lat, lon)])
    return values[0] if values else None


def _measure_chunk(chunk: Sequence[tuple[float, float]]
                   ) -> list[Optional[float]]:
    """Measure one batch, splitting it in half if the whole batch fails.

    A failed 3DEP request returns no samples at all, which is
    indistinguishable from "this ground has no data"; splitting isolates a
    single bad coordinate instead of losing the other nine. Recursion stops
    at single points (which return None and are retried through EPQS by the
    caller).
    """
    values = _samples_batch(chunk)
    _sleep()
    if len(values) < len(chunk):
        values += [None] * (len(chunk) - len(values))
    if any(value is not None for value in values) or len(chunk) == 1:
        return values
    half = len(chunk) // 2
    return _measure_chunk(chunk[:half]) + _measure_chunk(chunk[half:])


def elevations(points: Iterable[tuple[float, float]]
               ) -> list[Optional[float]]:
    """Elevations (feet) for (lat, lon) pairs, order preserved.

    Batches of 10; a batch that fails is split in half (down to single
    points) and finally falls back to EPQS. Unknown points come back as None
    and are simply left NULL in the database — unknown elevation is never
    treated as disqualifying.
    """
    pending = list(points)
    out: list[Optional[float]] = []
    for start in range(0, len(pending), MAX_POINTS_PER_REQUEST):
        chunk = pending[start:start + MAX_POINTS_PER_REQUEST]
        values = _measure_chunk(chunk)
        for (lat, lon), value in zip(chunk, values):
            if value is None:
                value = _epqs_point(lat, lon)
                _sleep()
            out.append(value)
    return out


# ---------------------------------------------------------------------------
# Enrichment / enforcement
# ---------------------------------------------------------------------------

def enrich_elevations(source: Optional[str] = None,
                      min_id: int = 0,
                      limit: Optional[int] = None,
                      only_missing: bool = True,
                      archive: bool = True,
                      write: bool = True,
                      conn=None) -> dict:
    """Fill ``elevation_ft`` for active rows with coordinates, then reap.

    Args:
        source: limit to one scraper source
        min_id: only rows with ``id > min_id`` (this run's new rows)
        limit: cap the number of rows measured
        only_missing: skip rows that already carry an elevation
        archive: archive rows measured below ``config.MIN_ELEVATION``
        write: measure and report only (no UPDATE, no archiving)
        conn: reuse an open connection (defaults to the app DB)
    Returns a stats dict (``by_state`` holds per-state measured/below counts).
    """
    own_conn = conn is None
    conn = conn or _ensure_db()
    min_elev = config.MIN_ELEVATION
    stats = {"target": 0, "measured": 0, "unknown": 0, "updated": 0,
             "below": 0, "archived": 0, "min_elevation": min_elev,
             "by_state": {}}
    try:
        where = ["status = 'active'", "latitude IS NOT NULL",
                 "longitude IS NOT NULL", "id > ?"]
        params: list = [min_id]
        if source:
            where.append("source = ?")
            params.append(source)
        if only_missing:
            where.append("elevation_ft IS NULL")
        sql = (f"SELECT id, latitude, longitude, state FROM properties "
               f"WHERE {' AND '.join(where)} ORDER BY id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        stats["target"] = len(rows)
        if not rows:
            return stats

        logger.info("Elevation: measuring %d row(s)%s", len(rows),
                    f" for {source}" if source else "")
        from datetime import datetime
        now = datetime.now().isoformat()
        # Checkpoint every WRITE_CHUNK rows: a 3DEP request costs ~7 s, so a
        # full pass is ~30 min and a single failed request must not throw the
        # whole pass away (a re-run skips rows that already carry a value).
        for start in range(0, len(rows), WRITE_CHUNK):
            chunk = rows[start:start + WRITE_CHUNK]
            values = elevations([(r[1], r[2]) for r in chunk])
            measured: list[tuple[int, float]] = []
            for (row_id, _, _, state), value in zip(chunk, values):
                if value is None:
                    stats["unknown"] += 1
                    continue
                measured.append((row_id, value))
                bucket = stats["by_state"].setdefault(
                    state or "?", {"measured": 0, "below": 0})
                bucket["measured"] += 1
                if min_elev > 0 and value < min_elev:
                    bucket["below"] += 1
                    stats["below"] += 1
            stats["measured"] += len(measured)
            if measured and write:
                conn.executemany(
                    "UPDATE properties SET elevation_ft = ?, last_updated = ? "
                    "WHERE id = ?",
                    [(round(value, 1), now, row_id)
                     for row_id, value in measured])
                conn.commit()
                stats["updated"] += len(measured)
            logger.info("Elevation: %d/%d rows measured",
                        min(start + WRITE_CHUNK, len(rows)), len(rows))

        if write and archive and min_elev > 0:
            stats["archived"] = archive_below_elevation(
                conn, min_elev, source=source)
        return stats
    finally:
        if own_conn:
            conn.close()
