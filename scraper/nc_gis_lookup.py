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

# NC1Map-specific FIPS codes (verified via NC1Map API, NOT Census Bureau)
# Source: https://www.nconemap.gov/arcgis/rest/services/NC1Map_Parcels/MapServer/1
NC_COUNTY_FIPS: dict[str, str] = {
    "alleghany": "005", "ashe": "009", "avery": "011",
    "burke": "023", "caldwell": "027", "cherokee": "039",
    "clay": "043", "cleveland": "045", "columbus": "047",
    "cumberland": "049", "graham": "075", "haywood": "087",
    "henderson": "089", "jackson": "099", "madison": "115",
    "mcdowell": "117", "mitchell": "121", "montgomery": "123",
    "polk": "149", "swain": "173", "transylvania": "177",
    "watauga": "189", "yancey": "199",
}

# Cache for repeated lookups
_cache: dict[str, Optional[dict]] = {}


# ---------------------------------------------------------------
# Query helper
# ---------------------------------------------------------------

def _nc1map_query(post_data: dict, timeout: int = 15) -> Optional[list[dict]]:
    """POST query to NC1Map Parcels and return list of feature dicts."""
    try:
        resp = curl_requests.Session(impersonate="chrome131").post(
            NC_ONEMAP_URL,
            data=post_data,
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

def _clean_features(feats: list[dict]) -> Optional[dict]:
    """Convert first feature's attributes to a clean dict."""
    if not feats:
        return None
    a = feats[0].get("attributes", {})
    result: dict[str, Any] = {}

    # Acreage
    gis_acres = a.get("gisacres")
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
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
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
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
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
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
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
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
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
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
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


def _lookup_parcel(parcel: str, county: Optional[str]) -> Optional[dict]:
    """Look up a parcel using NC OneMap with cntyfips-based strategies.
    
    Strategy:
      1. Try nparno = '37{fips}_{parcel}' with cntyfips filter
      2. Try altparno = parcel with cntyfips filter
      3. Try parno = parcel with cntyfips filter
      4. Fall back to parcel alone (no county filter)
    """
    if not parcel:
        return None
    fips = NC_COUNTY_FIPS.get((county or "").lower().strip())

    # Strategy 1: Try nparno = "37" + fips + "_" + parcel
    if fips:
        nparno = f"37{fips}_{parcel.strip()}"
        feats = _nc1map_query({
            "where": f"cntyfips='{fips}' AND nparno='{nparno}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=15)
        if feats:
            result = _clean_features(feats)
            if result and _county_matches(result.get("cntyname", ""), county):
                return result

    # Strategy 2: Try altparno = parcel with cntyfips filter
    if fips:
        feats = _nc1map_query({
            "where": f"cntyfips='{fips}' AND altparno='{parcel.strip()}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=15)
        if feats:
            result = _clean_features(feats)
            if result and _county_matches(result.get("cntyname", ""), county):
                return result

    # Strategy 3: Try parno = parcel with cntyfips filter
    if fips:
        feats = _nc1map_query({
            "where": f"cntyfips='{fips}' AND parno='{parcel.strip()}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=15)
        if feats:
            result = _clean_features(feats)
            if result:
                return result

    # Strategy 4: Query by parcel alone (no county filter)
    feats = _nc1map_query({
        "where": f"parno='{parcel.strip()}'",
        "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": "1",
    }, timeout=15)
    if feats:
        return _clean_features(feats)

    # Strategy 5: Query by altparno alone (no county filter)
    feats = _nc1map_query({
        "where": f"altparno='{parcel.strip()}'",
        "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,siteadd,ownname",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": "1",
    }, timeout=15)
    if feats:
        return _clean_features(feats)

    return None


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

    # URL links
    actual_county = parcel_data.get("cntyname") or gis_county
    parcel_ref = parcel_data.get("parno") if parcel_data else (rec.get("parcel_number") or "")

    from .config import GIS_PARCEL_URLS
    gc_lower = (actual_county or "").strip().title()
    county_gis = GIS_PARCEL_URLS.get(gc_lower, {})
    gis_url = None
    if county_gis:
        arc = county_gis.get("arcgis_url", "")
        fld = county_gis.get("field_name", "PARCELID")
        if arc and parcel_ref:
            gis_url = (f"{arc}/query?where={fld}%3D%27{parcel_ref}%27"
                       f"&outFields=*&returnGeometry=true&f=json")
    if not gis_url and parcel_ref:
        gis_url = (f"https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/"
                   f"MapServer/1/query?where=parno%3D%27{parcel_ref}%27"
                   f"&outFields=*&returnGeometry=true&f=json")

    rec["gis_url"] = gis_url

    street = rec.get("address") or address
    if street:
        parts = [s.strip() for s in [street, city, actual_county] if s and s.strip()]
        gmaps_q = "+".join(p for p in parts)
    elif parcel_ref:
        gmaps_q = f"{parcel_ref}+{actual_county}+NC"
    else:
        gmaps_q = None

    if gmaps_q:
        rec["google_maps_url"] = f"https://www.google.com/maps/search/{gmaps_q}"
        rec["google_maps_topo_url"] = (f"https://www.google.com/maps/search/{gmaps_q}"
                                       "/@?api=1&map_action=map&base=maps.terrain")
    else:
        rec["google_maps_url"] = None
        rec["google_maps_topo_url"] = None

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

    # Build query for properties that need GIS enrichment
    where = "acres IS NULL OR acres_source IS NULL OR acres_source = 'placeholder'"
    params = []

    if source:
        where += " AND source = ?"
        params.append(source)

    # Get properties with parcel numbers that need enrichment
    rows = conn.execute(
        f"SELECT id, source, county, parcel_number, address, city, acres_source "
        f"FROM properties WHERE {where}",
        params,
    ).fetchall()

    if not rows:
        logger.info("No properties need GIS enrichment")
        conn.close()
        return {"enriched": 0, "skipped_already_gis": len(rows), "skipped_no_parcel": 0, "failed": 0}

    enriched = 0
    skipped_no_parcel = 0
    skipped_already_gis = 0
    failed = 0

    for row_id, src, county, parcel, address, city, acres_src in rows:
        # Skip if already has GIS acres
        if acres_src == "gis":
            skipped_already_gis += 1
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

            # Update gis_url
            actual_county = (parcel_data.get("cntyname") or county) or ""
            parcel_ref = parcel_data.get("parno") or parcel_raw
            from .config import GIS_PARCEL_URLS
            gc_lower = (actual_county or "").strip().title()
            county_gis = GIS_PARCEL_URLS.get(gc_lower, {})
            gis_url = None
            if county_gis:
                arc = county_gis.get("arcgis_url", "")
                fld = county_gis.get("field_name", "PARCELID")
                if arc and parcel_ref:
                    gis_url = (f"{arc}/query?where={fld}%3D%27{parcel_ref}%27"
                               f"&outFields=*&returnGeometry=true&f=json")
            if not gis_url and parcel_ref:
                gis_url = (f"https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/"
                           f"MapServer/1/query?where=parno%3D%27{parcel_ref}%27"
                           f"&outFields=*&returnGeometry=true&f=json")
            update_fields["gis_url"] = gis_url

            # Update google maps URLs
            actual_address = address
            city_val = city
            if street := actual_address:
                parts = [s.strip() for s in [street, city_val, actual_county] if s and s.strip()]
                gmaps_q = "+".join(p for p in parts)
            elif parcel_ref:
                gmaps_q = f"{parcel_ref}+{actual_county}+NC"
            else:
                gmaps_q = None

            if gmaps_q:
                update_fields["google_maps_url"] = f"https://www.google.com/maps/search/{gmaps_q}"
                update_fields["google_maps_topo_url"] = (f"https://www.google.com/maps/search/{gmaps_q}"
                                                         "/@?api=1&map_action=map&base=maps.terrain")
            else:
                update_fields["google_maps_url"] = None
                update_fields["google_maps_topo_url"] = None

            # Build update SQL
            set_parts = ", ".join(f"{k} = ?" for k in update_fields if k != "id")
            conn.execute(
                f"UPDATE properties SET {set_parts} WHERE id = ?",
                [update_fields[k] for k in update_fields if k != "id"] + [row_id],
            )
            enriched += 1
            logger.info("Enriched #%s %s %s parcel=%s -> %sac", row_id, src, county, parcel_raw[:20], update_fields["acres"])
        else:
            failed += 1
            logger.debug("No GIS match for #%s %s county=%s parcel=%s", row_id, src, county, parcel_raw[:40])

        # Rate limit
        time.sleep(random.uniform(0.5, 1.5))

    conn.commit()
    conn.close()

    result = {
        "enriched": enriched,
        "skipped_already_gis": skipped_already_gis,
        "skipped_no_parcel": skipped_no_parcel,
        "failed": failed,
    }
    logger.info(f"Enrichment complete: {enriched} enriched, {skipped_already_gis} skipped(gis), "
                f"{skipped_no_parcel} skipped(no parcel), {failed} failed")
    return result


# Street type abbreviation map for NC addresses
_STREET_ABBREV = {
    "DRIVE": "DR", "ROAD": "RD", "STREET": "ST", "BLVD": "BLVD", "AVENUE": "AVE",
    "LANE": "LN", "CIRCLE": "CIR", "COURT": "CT", "PLACE": "PL", "WAY": "WAY",
    "DR": "DR", "RD": "RD", "ST": "ST", "AVE": "AVE", "LN": "LN", "CIR": "CIR",
    "CT": "CT", "PL": "PL", "BOULEVARD": "BLVD",
}

# County FIPS codes
_COUNTY_FIPS = {
    "alleghany": "005", "ashe": "009", "avery": "011", "buncombe": "015",
    "burke": "023", "caldwell": "027", "cherokee": "039", "clay": "043",
    "graham": "075", "haywood": "087", "henderson": "089", "jackson": "099",
    "madison": "115", "mcdowell": "117", "mitchell": "121", "polk": "149",
    "swain": "173", "transylvania": "177", "watauga": "189", "yancey": "199",
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
            timeout=15,
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
                "gis_url": None,
                "google_maps_url": None,
                "google_maps_topo_url": None,
            }

            # Build gis_url
            parcel_ref = result.get("parno", "")
            actual_county = result.get("cntyname") or county
            if parcel_ref:
                update_fields["gis_url"] = (
                    f"https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/"
                    f"MapServer/1/query?where=parno%3D%27{parcel_ref}%27"
                    f"&outFields=*&returnGeometry=true&f=json"
                )

            # Build gmaps URLs
            street = address
            if city:
                parts = [p.strip() for p in [street, city, actual_county] if p and p.strip()]
                gmaps_q = "+".join(parts)
            elif parcel_ref:
                gmaps_q = f"{parcel_ref}+{actual_county}+NC"
            else:
                gmaps_q = None

            if gmaps_q:
                update_fields["google_maps_url"] = f"https://www.google.com/maps/search/{gmaps_q}"
                update_fields["google_maps_topo_url"] = f"https://www.google.com/maps/search/{gmaps_q}/@?api=1&map_action=map&base=maps.terrain"

            set_parts = ", ".join(f"{k} = ?" for k in update_fields if k != "id")
            conn.execute(
                f"UPDATE properties SET {set_parts} WHERE id = ?",
                [update_fields[k] for k in update_fields if k != "id"] + [row_id],
            )
            enriched += 1
            logger.info("Hutchens enriched #%s %s parcel=%s -> %sac", row_id, county, result.get("parno")[:20], result["acres"])
        else:
            failed += 1
            logger.debug("Hutchens address match failed for #%s %s addr=%s", row_id, county, address[:40])

        time.sleep(random.uniform(0.5, 1.5))

    conn.commit()
    conn.close()

    result = {
        "enriched": enriched,
        "skipped_no_address": skipped_no_address,
        "failed": failed,
    }
    logger.info(f"Hutchens enrichment: {enriched} enriched, {skipped_no_address} skipped, {failed} failed")
    return result
