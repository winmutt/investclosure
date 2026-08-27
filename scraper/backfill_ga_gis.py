"""Decorate every GA (ga_publicnotice) property with GIS + map links.

For Georgia there is no statewide parcel hub; the canonical parcel viewer is
each county's qPublic (Schneider Corp) app. We set:

  * ``gis_url``            -> qPublic county parcel-search (or direct parcel
                             deep link for configured counties), via
                             ``get_ga_gis_url``.
  * ``google_maps_url``    -> Google Maps search scoped to ``GA`` (not NC).
  * ``google_maps_topo_url`` -> same with the terrain basemap.

NOTE: qPublic's application host (schneidercorp.com) returns HTTP 403 to
automated requests (Cloudflare WAF), so we cannot *fetch* parcel data
(owner / assessed value) programmatically -- only build clickable links.
"""
import sqlite3

from scraper import db as D
from scraper.config import config
from scraper.gis_urls import get_ga_gis_url
from scraper.nc_gis_lookup import (
    build_google_maps_url,
    build_google_maps_topo_url,
)
from urllib.parse import quote


def _ga_maps_query(address: str, parcel: str, county: str) -> str:
    """Build a Google Maps search string that actually scopes to Georgia.

    When the stored address is just our ``Parcel <n>`` placeholder (Towns
    County style, no street), search on the tax-map parcel number instead.
    """
    county_t = (county or "").strip().title()
    if address and not address.lower().startswith("parcel "):
        q = f"{address}, {county_t} County, GA"
    elif parcel:
        q = f"Tax Map & Parcel {parcel}, {county_t} County, GA"
    else:
        q = f"{county_t} County, GA"
    return "https://www.google.com/maps/search/?api=1&query=" + quote(q)


def decorate_ga():
    conn = D._ensure_db(config.db_path)
    rows = conn.execute(
        "SELECT id, county, parcel_number, address FROM properties "
        "WHERE source='ga_publicnotice'"
    ).fetchall()
    updated = 0
    for r in rows:
        pid, county, parcel, address = r["id"], r["county"], r["parcel_number"], r["address"]
        gis_url = get_ga_gis_url(county, parcel or "")
        maps_url = _ga_maps_query(address or "", parcel or "", county or "")
        topo_url = f"{maps_url}&map_action=map&base=maps.terrain"
        existing = conn.execute(
            "SELECT gis_url, google_maps_url, google_maps_topo_url FROM properties WHERE id=?",
            (pid,),
        ).fetchone()
        if existing and tuple(existing) == (gis_url, maps_url, topo_url):
            continue
        conn.execute(
            "UPDATE properties SET gis_url=?, google_maps_url=?, "
            "google_maps_topo_url=? WHERE id=?",
            (gis_url, maps_url, topo_url, pid),
        )
        updated += 1
    conn.commit()
    conn.close()
    return {"processed": len(rows), "updated": updated}


if __name__ == "__main__":
    print(decorate_ga())
