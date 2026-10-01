"""Backfill "commonly known as" addresses into NC public-notice rows.

NC foreclosure notices often hide the street address in a
"commonly known as / also known as <address>" clause after the legal
description, which the old parser ignored (address stayed NULL).

For every NC public-notice row:
  1. Re-extract the address from ``raw_source_text`` (CKA clause first).
  2. If the row still has no address but has a parcel, fill it from the
     NC OneMap ``siteadd``.
  3. Rebuild google/GIS links; when no parcel is stored, run an NC OneMap
     address search so a parcel-deep GIS link becomes possible
     (parcel-deep-or-nothing policy).

Run inside the container:
    python3 -m scraper.backfill_cka [--source nc_publicnotice]
"""
from __future__ import annotations
import argparse
import logging
import random
import re
import sqlite3
import time
from typing import Optional

from .config import config
from .nc_gis_lookup import (
    NC1MapService,
    build_gis_url,
    build_google_maps_url,
    build_google_maps_topo_url,
    search_address_in_nc1map,
    _nc1map_query,
    _COUNTY_FIPS,
    _STREET_ABBREV,
    _clean_features,
)
from .nc_publicnotice import extract_known_as_address

logger = logging.getLogger(__name__)


def _lookup_by_cka_address(address: str, county: str) -> dict | None:
    """Find the parcel whose OneMap siteadd matches a house-numbered street.

    The generic address search normalizes away the street number and can
    bind to any parcel on the street; here we query ``siteadd LIKE
    '<num> <street>%'`` (street type abbreviated, e.g. LANE -> LN) and
    require the house number back on the returned address, so a wrong
    same-street parcel never wins.
    """
    street = (address or "").split(",")[0].strip()
    m = re.match(r"(\d+)\s+(.+)", street)
    if not m:
        return None
    num, rest = m.group(1), m.group(2).strip().upper()
    fips = _COUNTY_FIPS.get((county or "").lower().strip())
    if not fips:
        return None
    words = re.sub(r"[^A-Z0-9 ]", "", rest).split()
    if words and words[-1] in _STREET_ABBREV:
        words[-1] = _STREET_ABBREV[words[-1]]
    for core in (
        " ".join(words),
        " ".join(words[:2]),
        " ".join(words[:-1]) if len(words) > 1 else "",
    ):
        if not core:
            continue
        feats = _nc1map_query({
            "where": f"cntyfips='{_COUNTY_FIPS[county.lower().strip()]}' "
                     f"AND siteadd LIKE '{num} {core}%'",
            "outFields": "parno,siteadd,gisacres,ownname,altparno,recareano",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "3",
        }, timeout=5)
        for feat in feats or []:
            data = _clean_features([feat])
            site = (data or {}).get("site_address") or ""
            if re.match(rf"^{num}\b", site):
                return data
        time.sleep(0.4)
    return None


def backfill_cka_addresses(source: str = "nc_publicnotice") -> dict:
    conn = sqlite3.connect(str(config.db_path))
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row

    state_ok = "state IS NULL OR UPPER(state) = 'NC'"
    if source and source != "all":
        where = f"source = ? AND ({state_ok})"
        params: list = [source]
    else:
        where = f"({state_ok}) AND source LIKE '%publicnotice%'"
        params = []

    rows = conn.execute(
        "SELECT id, source, county, address, city, parcel_number, acres, "
        "latitude, longitude, gis_url, google_maps_url, raw_source_text "
        f"FROM properties WHERE {where} ORDER BY id",
        params,
    ).fetchall()

    svc = NC1MapService()
    addr_updated = 0
    gis_updated = 0
    still_blank = 0

    for row in rows:
        row_id = row["id"]
        county = (row["county"] or "").strip()
        address = (row["address"] or "").strip()
        parcel = (row["parcel_number"] or "").strip()
        lat = row["latitude"]
        lng = row["longitude"]
        update: dict = {}

        cka = extract_known_as_address(row["raw_source_text"] or "")
        if cka and cka.lower() != address.lower():
            address = cka
            update["address"] = cka

        if not address and parcel:
            data = None
            for variant in dict.fromkeys([parcel, parcel.replace("-", "")]):
                if not variant:
                    continue
                try:
                    data = svc.by_parcel(variant, county=county)
                except Exception as e:
                    logger.warning("OneMap lookup failed #%s %s: %s",
                                   row_id, variant, e)
                if data:
                    break
                time.sleep(random.uniform(0.3, 0.6))
            if data:
                if data.get("site_address"):
                    address = data["site_address"]
                    update["address"] = address
                if data.get("parno") and not parcel:
                    update["parcel_number"] = data["parno"]
                if lat is None and data.get("latitude"):
                    lat = data["latitude"]
                    lng = data["longitude"]
                    update["latitude"] = lat
                    update["longitude"] = lng

        if address and "address" in update:
            gmaps = build_google_maps_url(lng, lat, address,
                                          row["city"] or None, county,
                                          state="NC")
            if gmaps:
                update["google_maps_url"] = gmaps
                update["google_maps_topo_url"] = build_google_maps_topo_url(
                    lat, lng, address, row["city"] or None, county,
                    state="NC")

        parcel_ref = update.get("parcel_number") or parcel
        if address and not parcel and lat is None:
            try:
                data = _lookup_by_cka_address(address, county)
                if not data:
                    data = search_address_in_nc1map(address, county)
            except Exception as e:
                logger.warning("OneMap address search failed #%s: %s",
                               row_id, e)
                data = None
            time.sleep(random.uniform(0.5, 0.9))
            if data:
                if data.get("parno"):
                    parcel_ref = data["parno"]
                    update["parcel_number"] = parcel_ref
                if data.get("acres") and row["acres"] is None:
                    update["acres"] = data["acres"]
                    update["acres_source"] = "gis"
                if data.get("latitude"):
                    lat = data["latitude"]
                    lng = data["longitude"]
                    update["latitude"] = lat
                    update["longitude"] = lng
                if data.get("owner_name"):
                    update["owner_name"] = data["owner_name"]

        gis = build_gis_url(lng, lat, parcel_ref, address, county, state="NC")
        if gis and gis != row["gis_url"]:
            update["gis_url"] = gis
            gis_updated += 1

        if not update:
            if not address:
                still_blank += 1
            continue

        set_parts = ", ".join(f"{k} = ?" for k in update)
        conn.execute(
            f"UPDATE properties SET {set_parts}, "
            "last_updated = CURRENT_TIMESTAMP WHERE id = ?",
            list(update.values()) + [row_id],
        )
        conn.commit()
        if "address" in update:
            addr_updated += 1
        logger.info("Backfilled #%s %s addr=%s parcel=%s gis=%s",
                    row_id, county, update.get("address"),
                    update.get("parcel_number"), bool(update.get("gis_url")))

    conn.commit()
    conn.close()
    result = {"address_updated": addr_updated, "gis_updated": gis_updated,
              "still_no_address": still_blank, "rows": len(rows)}
    logger.info("CKA backfill complete: %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="nc_publicnotice")
    args = ap.parse_args()
    print(backfill_cka_addresses(source=args.source))
