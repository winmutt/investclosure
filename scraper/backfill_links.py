"""Backfill coordinates + working map/GIS links for existing properties.

Re-runs NC OneMap parcel lookups for active properties and rebuilds
``latitude``, ``longitude``, ``gis_url``, ``google_maps_url`` and
``google_maps_topo_url`` using the new viewer-URL builders.

Strategy:
  * Active rows: full NC1Map lookup (parcel then address) so they get
    coordinates + working links.
  * Archived rows: rebuild links instantly from any existing coordinates /
    parcel / address — no API calls — so stale JSON REST URLs are replaced
    without burning NC1Map requests.
"""
import logging
import random
import sqlite3
import time

from .config import config
from .nc_gis_lookup import (
    _lookup_parcel,
    build_gis_url,
    build_google_maps_url,
    build_google_maps_topo_url,
    search_address_in_nc1map,
)

logger = logging.getLogger(__name__)


def _rebuild(lat, lng, parcel_ref, address_raw, city_raw, county_raw):
    gis_url = build_gis_url(lng, lat, parcel_ref)
    maps_url = build_google_maps_url(lng, lat, address_raw, city_raw, county_raw)
    topo_url = build_google_maps_topo_url(lng, lat, address_raw, city_raw, county_raw)
    return gis_url, maps_url, topo_url


def backfill_links(source: str = "all") -> dict:
    conn = sqlite3.connect(str(config.db_path))
    conn.execute("PRAGMA busy_timeout = 30000")

    where = "1=1"
    params: list = []
    if source and source != "all":
        where += " AND source = ?"
        params.append(source)

    rows = conn.execute(
        f"SELECT id, source, status, county, parcel_number, address, city, "
        f"latitude, longitude FROM properties WHERE {where} ORDER BY "
        f"CASE WHEN status='active' THEN 0 ELSE 1 END, id",
        params,
    ).fetchall()

    updated = 0
    api_calls = 0
    active_total = sum(1 for r in rows if r[2] == "active")

    for i, (row_id, src, status, county, parcel, address, city, lat0, lng0) in enumerate(rows, 1):
        parcel_raw = (parcel or "").strip()
        county_raw = (county or "").strip()
        address_raw = (address or "").strip() or None
        city_raw = (city or "").strip() or None

        lat = lat0
        lng = lng0
        parcel_ref = parcel_raw or None

        if not (lat and lng) and status == "active":
            # Active rows get a live lookup when coordinates are missing.
            if parcel_raw:
                pd = _lookup_parcel(parcel_raw, county_raw)
                api_calls += 1
                if pd:
                    lat = pd.get("latitude")
                    lng = pd.get("longitude")
                    if pd.get("parno"):
                        parcel_ref = pd["parno"]

            if (not lat or not lng) and address_raw and county_raw:
                pd = search_address_in_nc1map(address_raw, county_raw)
                api_calls += 1
                if pd:
                    lat = pd.get("latitude")
                    lng = pd.get("longitude")
                    if pd.get("parno"):
                        parcel_ref = pd["parno"]

            if api_calls > 0:
                time.sleep(random.uniform(0.3, 0.6))

        gis_url, maps_url, topo_url = _rebuild(
            lat, lng, parcel_ref, address_raw, city_raw, county_raw
        )

        # Only touch rows where something actually changes.
        existing = conn.execute(
            "SELECT latitude, longitude, gis_url, google_maps_url, google_maps_topo_url "
            "FROM properties WHERE id = ?",
            (row_id,),
        ).fetchone()
        if existing and tuple(existing) == (lat, lng, gis_url, maps_url, topo_url):
            continue

        conn.execute(
            "UPDATE properties SET latitude = ?, longitude = ?, gis_url = ?, "
            "google_maps_url = ?, google_maps_topo_url = ?, parcel_number = COALESCE(?, parcel_number) "
            "WHERE id = ?",
            (lat, lng, gis_url, maps_url, topo_url, parcel_ref or parcel_raw, row_id),
        )
        updated += 1
        logger.info(
            "Backfilled #%s %s [%s] coords=(%s, %s) gis=%s maps=%s",
            row_id, src, status, lat, lng, bool(gis_url), bool(maps_url),
        )

        if i % 25 == 0:
            conn.commit()

    conn.commit()
    conn.close()
    return {"updated": updated, "api_calls": api_calls, "active_processed": active_total}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all")
    args = ap.parse_args()
    print(backfill_links(source=args.source))
