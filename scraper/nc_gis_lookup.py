"""NC GIS parcel lookup — NC OneMap statewide service.

Queries the NC OneMap NC1Map_Parcels MapServer Layer 1 using POST.
The service requires POST requests for SQL WHERE clauses and returns JSON.

Key fields:
  - gisacres: GIS acreage (Double)
  - parno: parcel number (String)  
  - cntyfips: county FIPS code (3-digit, from NC1Map)
  - cntyname: county name
  - ownname: owner name
  - siteadd: site address
  - parusecd2/parusedsc2: land use code/description

Usage:
    service = NC1MapService()
    data = service.by_parcel("555602691434000", "Cherokee")
    # Returns: {"acres": 5.00, "parno": "555602691434000", "cntyname": "Cherokee", ...}
"""
from __future__ import annotations
import logging
import random
import re
import time
from typing import Optional, Any

from curl_cffi import requests as curl_requests

from .config import config

logger = logging.getLogger(__name__)

# NC OneMap Parcels MapServer Layer 1 (polygons)
NC_ONEMAP_URL = "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1/query"

# County FIPS codes (verified via NC1Map API queries, standard Census codes)
NC_COUNTY_FIPS: dict[str, str] = {
    "alleghany": "005", "ashe": "009", "avery": "011", "buncombe": "021",
    "burke": "023", "caldwell": "027", "catawba": "035", "cherokee": "039",
    "clay": "043", "cleveland": "045", "columbus": "047",
    "cumberland": "049", "franklin": "069", "graham": "075", "haywood": "087",
    "henderson": "089", "jackson": "099", "macon": "113", "madison": "115",
    "mcdowell": "111", "mitchell": "121", "montgomery": "123",
    "polk": "149", "rutherford": "161", "swain": "173", "transylvania": "175",
    "watauga": "189", "wilkes": "193", "yancey": "199",
}

# Cache for repeated lookups
_cache: dict[str, Optional[dict]] = {}


# ---------------------------------------------------------------
# Query helper
# ---------------------------------------------------------------

def _nc1map_query(post_data: dict, timeout: int = 15) -> Optional[list[dict]]:
    """POST query to NC1Map Parcels and return list of feature dicts.

    Geometry is always requested (EPSG:4326) so callers can compute a
    parcel centroid for map centering even if a caller only set
    ``returnGeometry`` to "false".
    """
    data = dict(post_data)
    data["returnGeometry"] = "true"
    data["outSR"] = "4326"
    try:
        resp = curl_requests.Session(impersonate="chrome131").post(
            NC_ONEMAP_URL,
            data=data,
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.debug("NC1Map HTTP %d", resp.status_code)
            return None
        data = resp.json()
        if data.get("error"):
            logger.debug("NC1Map error: %s", data["error"])
            return None
        feats = data.get("features", [])
        return [f for f in feats if f.get("attributes")] if feats else []
    except Exception as e:
        logger.debug("NC1Map query failed: %s", e)
        return None


# ---------------------------------------------------------------
# Feature cleaning
# ---------------------------------------------------------------

def _geometry_centroid(geometry: Optional[dict]) -> Optional[tuple[float, float]]:
    """Return (lng, lat) centroid of a polygon geometry (rings of [x, y]).

    ArcGIS polygons use ``rings`` where each ring is a list of [x, y]
    vertex pairs. The geographic centroid of an irregular parcel is
    approximated by averaging all exterior-ring vertices.
    """
    if not geometry:
        return None
    rings = geometry.get("rings")
    if not rings:
        return None
    pts = [p for ring in rings for p in ring]
    if not pts:
        return None
    n = len(pts)
    lng = sum(p[0] for p in pts) / n
    lat = sum(p[1] for p in pts) / n
    return round(lng, 6), round(lat, 6)


def _clean_features(feats: list[dict]) -> Optional[dict]:
    """Convert first feature's attributes to a clean dict."""
    if not feats:
        return None
    a = feats[0].get("attributes", {})
    result: dict[str, Any] = {}

    # Centroid from parcel polygon (EPSG:4326 from outSR=4326 request)
    centroid = _geometry_centroid(feats[0].get("geometry"))
    if centroid:
        result["longitude"] = centroid[0]
        result["latitude"] = centroid[1]

    # Acreage — Buncombe reports 0.0 in gisacres; fall back to recareano
    gis_acres = a.get("gisacres")
    try:
        gis_acres_float = float(gis_acres) if gis_acres not in (None, "") else 0.0
    except (ValueError, TypeError):
        gis_acres_float = 0.0
    if gis_acres_float <= 0:
        gis_acres = a.get("recareano")
    else:
        gis_acres = gis_acres_float
    if gis_acres is not None:
        try:
            result["acres"] = round(float(gis_acres), 2)
        except (ValueError, TypeError):
            return None
    if "acres" not in result:
        return None

    result["parno"] = a.get("parno")
    result["altparno"] = a.get("altparno")
    result["nparno"] = a.get("nparno")
    result["cntyname"] = a.get("cntyname")
    result["cntyfips"] = a.get("cntyfips")
    result["owner_name"] = str(a.get("ownname") or "").strip() or None

    site = a.get("siteadd")
    result["site_address"] = str(site).strip() or None

    lu_code = a.get("parusecd2")
    lu_desc = a.get("parusedsc2")
    if lu_code and lu_desc:
        result["land_use"] = f"{lu_code} — {lu_desc}"
    elif lu_code:
        result["land_use"] = str(lu_code)
    elif lu_desc:
        result["land_use"] = str(lu_desc)

    return result


# ---------------------------------------------------------------
# Service class
# ---------------------------------------------------------------

class NC1MapService:
    """Wrapper around NC1Map Parcels API.
    
    Strategy for finding a parcel by (county, parno):
      1. Query parno=XXXX with cntyfips=YYF (fast, accurate)
      2. Fall back to parno=XXXX alone (no county filter)
      3. Verify the returned parcel's county matches
    """

    def by_parcel(self, parcel: str, county: Optional[str] = None,
                  timeout: int = 15) -> Optional[dict]:
        """Look up a parcel and return {acres, parno, cntyname, ...}.
        
        Strategy:
          1. Try nparno = '37{fips}_{parcel}' with cntyfips filter
          2. Try altparno = parcel with cntyfips filter
          3. Try parno = parcel with cntyfips filter
          4. Fall back to parcel alone (no county filter)
          5. Verify returned parcel's county matches target
        """
        if not parcel:
            return None
        cache_key = ("parcel", parcel.strip(), county)
        if cache_key in _cache:
            return _cache[cache_key]

        clean = parcel.strip()
        fips = NC_COUNTY_FIPS.get((county or "").lower().strip())

        # Strategy 1: Try nparno = "37" + fips + "_" + parcel
        if fips:
            nparno = f"37{fips}_{clean}"
            feats = _nc1map_query({
                "where": f"cntyfips='{fips}' AND nparno='{nparno}'",
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": "1",
            }, timeout=timeout)
            if feats:
                result = _clean_features(feats)
                if result and _county_matches(result.get("cntyname", ""), county):
                    _cache[cache_key] = result
                    return result

        # Strategy 2: Try altparno = parcel with cntyfips filter
        if fips:
            feats = _nc1map_query({
                "where": f"cntyfips='{fips}' AND altparno='{clean}'",
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": "1",
            }, timeout=timeout)
            if feats:
                result = _clean_features(feats)
                if result and _county_matches(result.get("cntyname", ""), county):
                    _cache[cache_key] = result
                    return result

        # Strategy 3: Try parno = parcel with cntyfips filter
        if fips:
            feats = _nc1map_query({
                "where": f"cntyfips='{fips}' AND parno='{clean}'",
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": "1",
            }, timeout=timeout)
            if feats:
                result = _clean_features(feats)
                if county and result and not _county_matches(result.get("cntyname", ""), county):
                    logger.info("County mismatch %s/%s parcel=%s %s (fips=%s)",
                                county, "NC", clean, result.get("cntyname"), result.get("cntyfips"))
                    return None
                _cache[cache_key] = result
                return result

        # Strategy 4: Query by parcel alone (no county filter)
        feats = _nc1map_query({
            "where": f"parno='{clean}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=timeout)
        if feats:
            result = _clean_features(feats)
            if county and result and not _county_matches(result.get("cntyname", ""), county):
                logger.info("County mismatch %s/%s parcel=%s %s",
                            county, "NC", clean, result.get("cntyname"))
                return None
            _cache[cache_key] = result
            return result
        
        # Strategy 5: Query by altparno alone (no county filter)
        feats = _nc1map_query({
            "where": f"altparno='{clean}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=timeout)
        if feats:
            result = _clean_features(feats)
            if county and result and not _county_matches(result.get("cntyname", ""), county):
                logger.info("County mismatch %s/%s parcel=%s (alt) %s",
                            county, "NC", clean, result.get("cntyname"))
                return None
            _cache[cache_key] = result
            return result

        _cache[cache_key] = None
        return None


def _county_matches(gis_county: str, target: Optional[str]) -> bool:
    if not target:
        return True
    return gis_county.lower() == target.lower()


# ---------------------------------------------------------------
# Enrichment function
# ---------------------------------------------------------------

_enrich_svc: Optional[NC1MapService] = None


def _get_service() -> NC1MapService:
    global _enrich_svc
    if _enrich_svc is None:
        _enrich_svc = NC1MapService()
    return _enrich_svc


def enrich_kania_record(kania_rec: dict[str, Any]) -> dict[str, Any]:
    """Add NC OneMap parcel data to a Kania Law property record.
    
    Strategy:
      1. Query by (county, parcel) → parno + cntyfips
      2. Query by parcel alone → parno = 'XXXXX'
      3. Fall back to Google Maps if no GIS match
    
    Returns enriched dict with: acres, parcel_number, owner_name, land_use, 
    gis_url, google_maps_url, google_maps_topo_url
    """
    county = (kania_rec.get("county") or "").lower().strip()
    parcel_raw = (kania_rec.get("parcel_number") or "").strip()
    address = (kania_rec.get("address") or "").strip() or ""
    city = (kania_rec.get("city") or "").strip() or ""

    enriched = dict(kania_rec)
    parcel_data: Optional[dict] = None

    if parcel_raw:
        for candidate in re.split(r'[\s\t]+', parcel_raw):
            candidate = candidate.strip()
            if not candidate:
                continue
            svc = _get_service()
            parcel_data = _lookup_parcel(candidate, county)
            if parcel_data and parcel_data.get("acres", 0) > 0:
                break
            flat = re.sub(r'[-\s\t]', '', candidate)
            if flat != candidate and flat.strip():
                parcel_data = _lookup_parcel(flat, county)
                if parcel_data and parcel_data.get("acres", 0) > 0:
                    break

    if parcel_data:
        _apply_parcel_data(enriched, parcel_data, address, city, gis_county=county)
    else:
        logger.info("No GIS match for %s NC parcel=%s", county, parcel_raw[:40])

    return enriched


def _parcel_variants(parcel: str) -> list[str]:
    """Return normalization variants of a parcel number to try in lookups.

    NC OneMap stores some counties' parcel numbers without dashes (e.g.
    Buncombe '977539234200000') while scrapers may store the dashed form
    ('9775-39-2342-00000'). Exact-string queries fail on such mismatches,
    so we also try the original, dash-stripped, and punctuation-stripped
    variants.
    """
    cleaned = parcel.strip()
    variants: list[str] = []
    for v in (cleaned, cleaned.replace("-", "")):
        if v and v not in variants:
            variants.append(v)
    stripped = re.sub(r"[^A-Za-z0-9]", "", cleaned)
    if stripped and stripped not in variants:
        variants.append(stripped)
    return variants


def _lookup_parcel(parcel: str, county: Optional[str]) -> Optional[dict]:
    """Look up a parcel using NC OneMap with cntyfips-based strategies.

    Strategy (tried for the raw parcel and its normalized variants):
      1. Try nparno = '37{fips}_{parcel}' with cntyfips filter
      2. Try altparno = parcel with cntyfips filter
      3. Try parno = parcel with cntyfips filter
      4. Fall back to parcel alone (no county filter)
    """
    if not parcel:
        return None
    fips = NC_COUNTY_FIPS.get((county or "").lower().strip())

    for variant in _parcel_variants(parcel):
        # Strategy 1: Try nparno = "37" + fips + "_" + parcel
        if fips:
            nparno = f"37{fips}_{variant}"
            feats = _nc1map_query({
                "where": f"cntyfips='{fips}' AND nparno='{nparno}'",
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": "1",
            }, timeout=5)
            if feats:
                result = _clean_features(feats)
                if result and _county_matches(result.get("cntyname", ""), county):
                    return result

        # Strategy 2: Try altparno = parcel with cntyfips filter
        if fips:
            feats = _nc1map_query({
                "where": f"cntyfips='{fips}' AND altparno='{variant}'",
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": "1",
            }, timeout=5)
            if feats:
                result = _clean_features(feats)
                if result and _county_matches(result.get("cntyname", ""), county):
                    return result

        # Strategy 3: Try parno = parcel with cntyfips filter
        if fips:
            feats = _nc1map_query({
                "where": f"cntyfips='{fips}' AND parno='{variant}'",
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": "1",
            }, timeout=5)
            if feats:
                result = _clean_features(feats)
                if result:
                    return result

        # Strategy 4: Query by parcel alone (no county filter)
        feats = _nc1map_query({
            "where": f"parno='{variant}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=5)
        if feats:
            return _clean_features(feats)

        # Strategy 5: Query by altparno alone (no county filter)
        feats = _nc1map_query({
            "where": f"altparno='{variant}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=5)
        if feats:
            return _clean_features(feats)

    return None


# ArcGIS Online Map Viewer layer URL for NC1Map Parcels (polygons layer)
NC_ONEMAP_VIEWER_URL = (
    "https://www.arcgis.com/apps/mapviewer/index.html"
    "?url=https%3A%2F%2Fservices.nconemap.gov%2Fsecure%2Frest%2Fservices%2FNC1Map_Parcels%2FMapServer%2F1"
)


def _is_busted_gis_url(url: Optional[str]) -> bool:
    """True when a stored GIS URL is a dead REST query or JSON endpoint.

    Old enrichment code stored ``.../query?where=...&f=json`` URLs that
    return raw JSON (or 404 from dead services6.arcgis.com) instead of a
    human-viewable map.
    """
    if not url:
        return False
    if "services6.arcgis.com" in url:
        return True
    if "/query" in url and ("f=json" in url or "outFields" in url):
        return True
    return False


def _is_busted_maps_url(url: Optional[str]) -> bool:
    """True when a stored Google Maps URL is not a search/place link."""
    if not url:
        return False
    if url.startswith("https://www.google.com/maps/search/"):
        return False
    if "google.com/maps" in url:
        return False
    return True


def build_gis_url(lng: Optional[float], lat: Optional[float], parcel: Optional[str]) -> Optional[str]:
    """Build a human-viewable GIS viewer URL that shows the parcel.

    Uses the ArcGIS Online Map Viewer with the NC1Map Parcels layer. When
    coordinates are available the map is centered and zoomed on the parcel;
    otherwise it falls back to a Google Maps parcel search.
    """
    if lng and lat:
        return f"{NC_ONEMAP_VIEWER_URL}&center={lng:.6f},{lat:.6f}&level=16"
    if parcel:
        return f"https://www.google.com/maps/search/parcel+{parcel}+in+NC"
    return None


def build_google_maps_url(lng: Optional[float], lat: Optional[float],
                          address: Optional[str], city: Optional[str],
                          county: Optional[str]) -> Optional[str]:
    """Build a Google Maps URL that visually locates the property.

    Coordinates produce a precise pin; otherwise the address + city +
    county + state text search is used.
    """
    if lng and lat:
        return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"
    parts = [p.strip() for p in [address, city, county, "NC"] if p and p.strip()]
    if parts:
        q = "+".join(p.replace(" ", "+") for p in parts)
        return f"https://www.google.com/maps/search/?api=1&query={q}"
    return None


def build_google_maps_topo_url(lng: Optional[float], lat: Optional[float],
                               address: Optional[str], city: Optional[str],
                               county: Optional[str]) -> Optional[str]:
    """Build a Google Maps URL with the terrain/topographic basemap."""
    base = build_google_maps_url(lng, lat, address, city, county)
    if not base:
        return None
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}map_action=map&base=maps.terrain"


def _apply_parcel_data(rec: dict, parcel_data: dict, address: str, city: str, gis_county: str) -> None:
    """Apply GIS parcel data to a record."""
    if parcel_data.get("acres", 0) > 0:
        rec["acres"] = parcel_data["acres"]
        rec["parcel_number"] = parcel_data["parno"]
        rec["acres_source"] = "gis"
        if parcel_data.get("owner_name"):
            rec["owner_name"] = parcel_data["owner_name"]
        if parcel_data.get("land_use") and parcel_data["land_use"]:
            rec["land_use"] = parcel_data["land_use"]
        else:
            rec["land_use"] = None
    else:
        rec["acres_source"] = "placeholder"

    actual_county = parcel_data.get("cntyname") or gis_county
    parcel_ref = parcel_data.get("parno") if parcel_data else (rec.get("parcel_number") or "")

    # Parcel centroid coordinates (used to center map viewers on the parcel)
    if parcel_data.get("latitude") and parcel_data.get("longitude"):
        rec["latitude"] = parcel_data["latitude"]
        rec["longitude"] = parcel_data["longitude"]

    rec["gis_url"] = build_gis_url(
        parcel_data.get("longitude"), parcel_data.get("latitude"), parcel_ref
    )

    street = rec.get("address") or address
    rec["google_maps_url"] = build_google_maps_url(
        parcel_data.get("latitude"), parcel_data.get("longitude"),
        street, city, actual_county,
    )
    rec["google_maps_topo_url"] = build_google_maps_topo_url(
        parcel_data.get("latitude"), parcel_data.get("longitude"),
        street, city, actual_county,
    )

    rec["gis_county"] = actual_county


def enrich_properties(source: Optional[str] = None) -> dict:
    """Enrich properties in DB with GIS data.
    
    Queries NC OneMap for all properties that have a parcel_number but no GIS acres.
    Upserts the enriched data back into the properties table.
    
    Args:
        source: Optional source filter (e.g., "kania_law", "hutchens_law"). Defaults to all.
    
    Returns:
        dict with counts: enriched, skipped_no_parcel, skipped_already_gis, failed
    """
    try:
        import sqlite3
    except ImportError:
        logger.warning("sqlite3 not available, skipping enrich step")
        return {"error": "sqlite3 not available"}

    db_path = str(config.db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 30000")

    # Build query for properties that need GIS enrichment.
    # NOTE: parentheses are required — SQLite binds AND tighter than OR, so
    # the whole OR chain must be wrapped before the source filter is appended.
    # Also re-process rows whose stored map/GIS links are stale/busted
    # (old code stored JSON-returning REST query URLs).
    where = (
        "(acres IS NULL OR acres_source IS NULL OR acres_source = 'placeholder')"
        " OR (parcel_number IS NOT NULL AND parcel_number != '' AND (gis_url IS NULL OR gis_url = ''))"
        " OR (parcel_number IS NOT NULL AND parcel_number != '' AND ("
        "     gis_url LIKE '%services6.arcgis.com%'"
        "     OR gis_url LIKE '%/query%f=json%'"
        "     OR google_maps_url LIKE '%/maps/search/%' AND google_maps_url NOT LIKE '%?api=1%'"
        "))"
    )
    where = f"({where})"
    params = []

    if source:
        where += " AND source = ?"
        params.append(source)

    # Get properties with parcel numbers that need enrichment
    rows = conn.execute(
        f"SELECT id, source, county, parcel_number, address, city, acres_source, gis_url "
        f"FROM properties WHERE {where}",
        params,
    ).fetchall()

    if not rows:
        logger.info("No properties need GIS enrichment")
        conn.close()
        return {"enriched": 0, "skipped_already_gis": 0, "skipped_no_parcel": 0, "failed": 0}

    enriched = 0
    skipped_no_parcel = 0
    failed = 0
    MAX_ENRICH = 500

    for row_id, src, county, parcel, address, city, acres_src, gis_url in rows:
        # Stop after MAX_ENRICH to avoid long-running enrichment
        if enriched + skipped_no_parcel + failed >= MAX_ENRICH:
            logger.info("Enrichment limit reached (%d), stopping", MAX_ENRICH)
            break
            continue

        parcel_raw = (parcel or "").strip()
        if not parcel_raw:
            skipped_no_parcel += 1
            continue

        # Lookup parcel
        parcel_data = _lookup_parcel(parcel_raw, county)

        if parcel_data and parcel_data.get("acres", 0) > 0:
            # Build update dict
            update_fields = {
                "id": row_id,
                "acres": parcel_data["acres"],
                "acres_source": "gis",
                "gis_county": parcel_data.get("cntyname") or county,
                "owner_name": parcel_data.get("owner_name"),
            }

            # Update parcel number to NC1Map's canonical form
            if parcel_data.get("parno"):
                update_fields["parcel_number"] = parcel_data["parno"]

            # Update land use
            if parcel_data.get("land_use"):
                update_fields["land_use"] = parcel_data["land_use"]
            else:
                update_fields["land_use"] = None

            # Update coordinates from parcel centroid
            if parcel_data.get("latitude") and parcel_data.get("longitude"):
                update_fields["latitude"] = parcel_data["latitude"]
                update_fields["longitude"] = parcel_data["longitude"]

            # Update gis_url (human-viewable Map Viewer)
            actual_county = (parcel_data.get("cntyname") or county) or ""
            parcel_ref = parcel_data.get("parno") or parcel_raw
            update_fields["gis_url"] = build_gis_url(
                parcel_data.get("longitude"), parcel_data.get("latitude"), parcel_ref
            )

            # Update google maps URLs
            update_fields["google_maps_url"] = build_google_maps_url(
                parcel_data.get("latitude"), parcel_data.get("longitude"),
                address, city, actual_county,
            )
            update_fields["google_maps_topo_url"] = build_google_maps_topo_url(
                parcel_data.get("latitude"), parcel_data.get("longitude"),
                address, city, actual_county,
            )

            # Build update SQL
            set_parts = ", ".join(f"{k} = ?" for k in update_fields if k != "id")
            conn.execute(
                f"UPDATE properties SET {set_parts} WHERE id = ?",
                [update_fields[k] for k in update_fields if k != "id"] + [row_id],
            )
            conn.commit()
            enriched += 1
            logger.info("Enriched #%s %s %s parcel=%s -> %sac", row_id, src, county, parcel_raw[:20], update_fields["acres"])
        else:
            failed += 1
            logger.debug("No GIS match for #%s %s county=%s parcel=%s", row_id, src, county, parcel_raw[:40])

        # Rate limit
        time.sleep(random.uniform(0.3, 0.6))

    conn.commit()
    conn.close()

    result = {
        "enriched": enriched,
        "skipped_no_parcel": skipped_no_parcel,
        "failed": failed,
    }
    logger.info(f"Enrichment complete: {enriched} enriched, "
                f"{skipped_no_parcel} skipped(no parcel), {failed} failed")
    return result


# Street type abbreviation map for NC addresses
_STREET_ABBREV = {
    "DRIVE": "DR", "ROAD": "RD", "STREET": "ST", "BLVD": "BLVD", "AVENUE": "AVE",
    "LANE": "LN", "CIRCLE": "CIR", "COURT": "CT", "PLACE": "PL", "WAY": "WAY",
    "DR": "DR", "RD": "RD", "ST": "ST", "AVE": "AVE", "LN": "LN", "CIR": "CIR",
    "CT": "CT", "PL": "PL", "BOULEVARD": "BLVD",
}

# County FIPS codes (verified via NC1Map API queries, standard Census codes)
_COUNTY_FIPS = {
    "alleghany": "005", "ashe": "009", "avery": "011", "buncombe": "021",
    "burke": "023", "caldwell": "027", "catawba": "035", "cherokee": "039",
    "clay": "043", "cleveland": "045", "franklin": "069", "graham": "075",
    "haywood": "087", "henderson": "089", "jackson": "099", "macon": "113",
    "madison": "115", "mcdowell": "111", "mitchell": "121", "polk": "149",
    "rutherford": "161", "swain": "173", "transylvania": "175", "watauga": "189",
    "wilkes": "193", "yancey": "199",
}


def _normalize_address(addr: str) -> Optional[str]:
    """Normalize a street address for NC1Map searching.
    
    Examples:
      '16 Overlook Drive' -> 'OVERLOOK DR'
      '202 Mountain View Street' -> 'MOUNTAIN VIEW ST'
      '155 Old County Home Road' -> 'OLD COUNTY HOME RD'
    """
    addr_clean = re.sub(r"^\s*\d+\s+", "", addr.strip()).upper()
    words = addr_clean.split()
    
    if words and words[-1] in _STREET_ABBREV:
        words[-1] = _STREET_ABBREV[words[-1]]
    
    if len(words) >= 3:
        return " ".join(words[-3:])
    elif len(words) == 2:
        return f"{words[0][:3]} {words[1]}"
    return " ".join(words[:3]) if words else None


def search_address_in_nc1map(address: str, county: str) -> Optional[dict]:
    """Try to find a parcel by normalized street address in NC OneMap.
    
    Returns enriched data dict if found, else None.
    """
    if not address or not county:
        return None
    
    normalized = _normalize_address(address)
    if not normalized:
        return None
    
    fips = _COUNTY_FIPS.get((county or "").lower().strip())
    if not fips:
        return None
    
    # Query with wildcards between words
    like_parts = " ".join(normalized.split())
    # Try full pattern first, then abbreviated
    for pattern in [like_parts, "%".join(like_parts.split()[:2])]:
        if not pattern:
            continue
        where_str = f"cntyfips='{fips}' AND siteadd LIKE '%{pattern}%'"
        resp = _nc1map_query(
            {
                "where": where_str,
                "outFields": "parno,siteadd,gisacres,ownname,sourceref,altparno",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": "3",
            },
            timeout=5,
        )
        if resp:
            result = _clean_features(resp)
            if result and result.get("acres", 0) > 0:
                return result
    
    return None


def enrich_hutchens_properties() -> dict:
    """Enrich Hutchens Law properties using address-based NC1Map lookup.
    
    Hutchens records have deed_book but no parcel_number.
    This function normalizes the address and searches NC1Map parcels.
    """
    try:
        import sqlite3
    except ImportError:
        logger.warning("sqlite3 not available, skipping Hutchens enrichment")
        return {"error": "sqlite3 not available"}

    db_path = str(config.db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 30000")

    # Get Hutchens records without GIS but with address
    rows = conn.execute(
        "SELECT id, address, city, county, deed_book "
        "FROM properties WHERE source='hutchens_law' "
        "AND acres IS NULL AND address IS NOT NULL AND address != ''"
    ).fetchall()

    if not rows:
        logger.info("No Hutchens properties need address enrichment")
        conn.close()
        return {"enriched": 0, "skipped_no_address": 0, "failed": 0}

    enriched = 0
    skipped_no_address = 0
    failed = 0

    for row_id, address, city, county, deed_book in rows:
        if not address:
            skipped_no_address += 1
            continue

        result = search_address_in_nc1map(address, county)
        if result and result.get("acres", 0) > 0:
            update_fields = {
                "id": row_id,
                "acres": result["acres"],
                "acres_source": "gis",
                "parcel_number": result.get("parno"),
                "owner_name": result.get("owner_name"),
                "land_use": None,
                "gis_county": result.get("cntyname") or county,
            }

            # Update coordinates from parcel centroid
            if result.get("latitude") and result.get("longitude"):
                update_fields["latitude"] = result["latitude"]
                update_fields["longitude"] = result["longitude"]

            # Build gis_url (human-viewable Map Viewer)
            parcel_ref = result.get("parno", "")
            actual_county = result.get("cntyname") or county
            update_fields["gis_url"] = build_gis_url(
                result.get("longitude"), result.get("latitude"), parcel_ref
            )

            # Build gmaps URLs
            update_fields["google_maps_url"] = build_google_maps_url(
                result.get("latitude"), result.get("longitude"),
                address, city, actual_county,
            )
            update_fields["google_maps_topo_url"] = build_google_maps_topo_url(
                result.get("latitude"), result.get("longitude"),
                address, city, actual_county,
            )

            set_parts = ", ".join(f"{k} = ?" for k in update_fields if k != "id")
            conn.execute(
                f"UPDATE properties SET {set_parts} WHERE id = ?",
                [update_fields[k] for k in update_fields if k != "id"] + [row_id],
            )
            conn.commit()
            enriched += 1
            logger.info("Hutchens enriched #%s %s parcel=%s -> %sac", row_id, county, result.get("parno")[:20], result["acres"])
        else:
            failed += 1
            logger.debug("Hutchens address match failed for #%s %s addr=%s", row_id, county, address[:40])

        time.sleep(random.uniform(0.3, 0.6))

    conn.commit()
    conn.close()

    result = {
        "enriched": enriched,
        "skipped_no_address": skipped_no_address,
        "failed": failed,
    }
    logger.info(f"Hutchens enrichment: {enriched} enriched, {skipped_no_address} skipped, {failed} failed")
    return result
