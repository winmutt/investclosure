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
from datetime import date
from typing import Optional, Any
from urllib.parse import quote

from curl_cffi import requests as curl_requests

from .config import config
from .gis_urls import (
    get_gis_viewer_url,
    get_ga_gis_url,
    get_nconemap_viewer_url,
    NC_ONEMAP_VIEWER_URL,
)
from .county_parcel import resolve_county_tax_id

logger = logging.getLogger(__name__)

# NC OneMap Parcels MapServer Layer 1 (polygons)
NC_ONEMAP_URL = "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1/query"

# County FIPS codes (verified via NC1Map API queries, standard Census codes)
NC_COUNTY_FIPS: dict[str, str] = {
    "alleghany": "005", "ashe": "009", "avery": "011", "buncombe": "021",
    "burke": "023", "catawba": "035", "cherokee": "039",
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
        parcel_data = _lookup_parcel_multi(parcel_raw, county)

    if parcel_data:
        _apply_parcel_data(enriched, parcel_data, address, city, gis_county=county, state="NC")
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

        # Strategy 4: Query by parcel alone (still require county to match,
        # otherwise a bare token can match an unrelated parcel in another county).
        feats = _nc1map_query({
            "where": f"parno='{variant}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=5)
        if feats:
            result = _clean_features(feats)
            if result and _county_matches(result.get("cntyname", ""), county):
                return result

        # Strategy 5: Query by altparno alone (require county match, same reason).
        feats = _nc1map_query({
            "where": f"altparno='{variant}'",
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": "1",
        }, timeout=5)
        if feats:
            result = _clean_features(feats)
            if result and _county_matches(result.get("cntyname", ""), county):
                return result

    return None


def _split_parcels(parcel_raw: str) -> list[str]:
    """Split a parcel field that may pack several PINs into one value.

    Kania Law (and others) sometimes store multiple parcel numbers in a single
    field, separated by spaces, commas, slashes or semicolons
    (e.g. ``'13196004051 132954734805'`` or ``'32276 32342 9140 9139'``).
    """
    return [p.strip() for p in re.split(r"[\s,;/]+", parcel_raw or "") if p.strip()]


def _lookup_parcel_multi(parcel_raw: str, county: Optional[str]) -> Optional[dict]:
    """Look up one or many parcel numbers stored in a single field.

    Each parcel is queried against NC OneMap; matching features are merged so
    the returned acreage is the *sum* across all matched parcels (the total
    property acreage) while the highest-acreage parcel is used as the primary
    (map pin + canonical parcel number).

    Duplicate PINs that resolve to the *same* physical parcel (e.g. a parcel
    number listed alongside its altparno) are counted only once, so a property
    is never double-counted.
    """
    candidates = _split_parcels(parcel_raw)
    if not candidates:
        return None

    # Authoritative resolution: if the county publishes its own parcel service
    # that maps the county tax ID -> NC statewide PIN, use that PIN directly.
    # This avoids the fragile NC OneMap address search (which can bind the
    # property to the wrong parcel on the same street).
    if county:
        for cand in candidates:
            res = resolve_county_tax_id(county, cand)
            if res and res.get("pin"):
                data = _lookup_parcel_multi(res["pin"], county)
                if data:
                    logger.info("Resolved %s/%s tax id %s -> PIN %s via county service",
                                county, "NC", cand, res["pin"])
                    return data

    seen_parnos: set = set()
    matches: list[dict] = []
    for cand in candidates:
        for variant in _parcel_variants(cand):
            data = _lookup_parcel(variant, county)
            if data and data.get("acres", 0) > 0:
                parno = data.get("parno")
                # Same physical parcel already counted under another PIN/alt-PIN
                if parno and parno in seen_parnos:
                    break
                if parno:
                    seen_parnos.add(parno)
                matches.append(data)
                break
    if not matches:
        return None
    primary = max(matches, key=lambda d: d.get("acres", 0) or 0)
    total = round(sum(d.get("acres", 0) or 0 for d in matches), 2)
    merged = dict(primary)
    merged["acres"] = total
    return merged


# ArcGIS Online Map Viewer layer URL for NC1Map Parcels (polygons layer) is
# imported from gis_urls as NC_ONEMAP_VIEWER_URL.


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


def build_gis_url(lng: Optional[float] = None, lat: Optional[float] = None,
                  parcel: Optional[str] = None, address: Optional[str] = None,
                  county: Optional[str] = None, state: Optional[str] = None) -> Optional[str]:
    """Build a human-viewable GIS viewer URL for the parcel/address.

    Georgia (``state='GA'``) properties use the county's qPublic (Schneider
    Corp) parcel-search page, since NC OneMap does not cover Georgia.
    Tennessee (``state='TN'``) uses the TNMap Assessment viewer /
    TN Comptroller TPAD deep link. North Carolina uses the NC OneMap
    statewide ArcGIS Map Viewer.
    """
    s = str(state or "").strip().upper()
    if s == "GA":
        return get_ga_gis_url(county, parcel)
    if s == "TN":
        # Prefer TNMap Assessment viewer. When GISLINK-style parcel is
        # available the TNMap enrichment sets a TPAD deep link directly;
        # this helper builds a pre-enrichment search link.
        from .gis_urls import get_tn_gis_url
        return get_tn_gis_url(county, parcel, lng, lat)

    # North Carolina uses the NC OneMap statewide parcel layer through our
    # same-origin viewer/proxy. Center on parcel coordinates when available.
    if s == "NC":
        return get_nconemap_viewer_url(lng, lat, parcel, county)

    # Fallback for other/unspecified states: treat as NC OneMap with
    # state-aware query so we don't hardcode "NC".
    if lng is not None and lat is not None:
        return f"{NC_ONEMAP_VIEWER_URL}&center={lng:.6f},{lat:.6f}&level=16"
    if address:
        st = s or "NC"
        q = " ".join(p for p in [address, county, st] if p and p.strip())
        return f"{NC_ONEMAP_VIEWER_URL}&find={quote(q)}"
    if parcel:
        return NC_ONEMAP_VIEWER_URL
    if county:
        st = s or "NC"
        return f"{NC_ONEMAP_VIEWER_URL}&find={quote(f'{county.strip()} {st}')}"
    return None


def build_google_maps_url(lng: Optional[float] = None, lat: Optional[float] = None,
                           address: Optional[str] = None, city: Optional[str] = None,
                           county: Optional[str] = None, state: Optional[str] = None) -> Optional[str]:
    """Build a Google Maps URL that visually locates the property.

    A street address (with city/county) is preferred because coordinate pins
    are occasionally wrong; coordinates are only used as a fallback when no
    address is available. ``state`` defaults to ``NC`` (the primary scrape
    region) but is set to ``GA`` for Georgia properties so the search is not
    mis-scoped to North Carolina.
    """
    st = (state or "NC").strip() or "NC"
    parts = [p for p in [address, city, county, st] if p and p.strip()]
    if parts:
        q = "+".join(p.replace(" ", "+") for p in parts)
        return f"https://www.google.com/maps/search/?api=1&query={q}"
    if lng and lat:
        return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"
    return None


def build_google_maps_topo_url(lng: Optional[float], lat: Optional[float],
                                 address: Optional[str], city: Optional[str],
                                 county: Optional[str], state: Optional[str] = None) -> Optional[str]:
    """Build a Google Maps URL with the terrain/topographic basemap."""
    base = build_google_maps_url(lng, lat, address, city, county, state=state)
    if not base:
        return None
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}map_action=map&base=maps.terrain"


def build_satellite_url(lng: Optional[float] = None, lat: Optional[float] = None,
                        address: Optional[str] = None, city: Optional[str] = None,
                        county: Optional[str] = None, state: Optional[str] = None) -> Optional[str]:
    """Build a Google Maps satellite/aerial photo URL centered on the parcel.

    Uses the classic Google Maps ``t=k`` (satellite) parameter, which forces an
    aerial/photo basemap regardless of the viewer's default layer. When
    coordinates are available it drops a pin at the exact parcel; otherwise it
    searches the street address.
    """
    if lat is not None and lng is not None:
        return f"https://maps.google.com/maps?q={lat:.6f},{lng:.6f}&z=18&t=k"
    st = (state or "NC").strip() or "NC"
    parts = [p for p in [address, city, county, st] if p and p.strip()]
    if parts:
        q = "+".join(p.replace(" ", "+") for p in parts)
        return f"https://maps.google.com/maps?q={q}&z=15&t=k"
    return None


def build_street_view_url(lng: Optional[float] = None, lat: Optional[float] = None,
                          address: Optional[str] = None, city: Optional[str] = None,
                          county: Optional[str] = None, state: Optional[str] = None) -> Optional[str]:
    """Build a Google Street View photo URL for the parcel (when available).

    Uses the Street View URL scheme (``map_action=pano``). With coordinates it
    opens the panorama at the parcel's location; otherwise it searches the
    address. Google shows a "no imagery" notice when no street-level photo
    exists for that spot.
    """
    if lat is not None and lng is not None:
        return (f"https://www.google.com/maps/@?api=1&map_action=pano"
                f"&viewpoint={lat:.6f},{lng:.6f}&heading=0&pitch=0&fov=80")
    st = (state or "NC").strip() or "NC"
    parts = [p for p in [address, city, county, st] if p and p.strip()]
    if parts:
        q = "+".join(p.replace(" ", "+") for p in parts)
        return f"https://www.google.com/maps/?api=1&query={q}&map_action=pano"
    return None


def _apply_parcel_data(rec: dict, parcel_data: dict, address: str, city: str, gis_county: str, state: str = "NC") -> None:
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
        parcel_data.get("longitude"), parcel_data.get("latitude"),
        parcel_ref, address, gis_county, state=state,
    )

    street = rec.get("address") or address
    rec["google_maps_url"] = build_google_maps_url(
        parcel_data.get("longitude"), parcel_data.get("latitude"),
        street, city, actual_county, state=state,
    )
    rec["google_maps_topo_url"] = build_google_maps_topo_url(
        parcel_data.get("latitude"), parcel_data.get("longitude"),
        street, city, actual_county, state=state,
    )
    rec["google_maps_satellite_url"] = build_satellite_url(
        parcel_data.get("longitude"), parcel_data.get("latitude"),
        street, city, actual_county, state=state,
    )
    rec["google_maps_street_url"] = build_street_view_url(
        parcel_data.get("longitude"), parcel_data.get("latitude"),
        street, city, actual_county, state=state,
    )

    rec["gis_county"] = actual_county


def enrich_properties(source: Optional[str] = None) -> dict:
    """Enrich properties in DB with GIS data.
    
    Queries NC OneMap for all properties that have a parcel_number but no GIS acres.
    Upserts the enriched data back into the properties table.
    
    Args:
        source: Optional source filter (e.g., "kania_law"). Defaults to all.
    
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
    # (old code stored JSON-returning REST query URLs), and any row that has
    # a street address but is missing a map/GIS link.
    where = (
        "(acres IS NULL OR acres_source IS NULL OR acres_source = 'placeholder')"
        " OR (parcel_number IS NOT NULL AND parcel_number != '' AND ("
        "     gis_url IS NULL OR gis_url = ''"
        "     OR google_maps_url IS NULL OR google_maps_url = ''))"
        " OR (address IS NOT NULL AND address != '' AND ("
        "     gis_url IS NULL OR gis_url = ''"
        "     OR google_maps_url IS NULL OR google_maps_url = ''))"
        " OR (parcel_number IS NOT NULL AND parcel_number != '' AND ("
        "     gis_url LIKE '%services6.arcgis.com%'"
        "     OR gis_url LIKE '%/query%f=json%'"
        "     OR (google_maps_url LIKE '%/maps/search/%' AND google_maps_url NOT LIKE '%?api=1%')"
        "))"
    )
    where = f"({where})"
    params = []

    if source:
        where += " AND source = ?"
        params.append(source)

    # Get properties (with a parcel OR an address) that need GIS enrichment
    rows = conn.execute(
        f"SELECT id, source, county, parcel_number, address, city, state, acres_source, gis_url, google_maps_url "
        f"FROM properties WHERE {where}",
        params,
    ).fetchall()

    if not rows:
        logger.info("No properties need GIS enrichment")
        conn.close()
        return {"enriched": 0, "url_updated": 0, "skipped_no_parcel": 0, "failed": 0}

    enriched = 0
    url_updated = 0
    skipped_no_parcel = 0
    failed = 0
    MAX_ENRICH = 500

    for row in rows:
        if enriched + url_updated + failed >= MAX_ENRICH:
            logger.info("Enrichment limit reached (%d), stopping", MAX_ENRICH)
            break

        (row_id, src, county, parcel, address, city, state,
         acres_src, gis_url, gmaps_url) = row

        parcel_raw = (parcel or "").strip()
        address = (address or "").strip()
        city = (city or "").strip()

        if not parcel_raw and not address:
            skipped_no_parcel += 1
            continue

        # 1) Prefer an authoritative county-specific GIS lookup (Cherokee, ...).
        #    Falls back to NC OneMap parcel/address search.
        parcel_data = None
        county_tag = None
        if parcel_raw or address:
            parcel_data = county_specific_lookup(county, parcel_raw, address)
            if parcel_data and parcel_data.get("confidence") == "high":
                county_tag = parcel_data.get("source_tag") or "county_gis"
            else:
                parcel_data = None
        if not parcel_data and parcel_raw:
            parcel_data = _lookup_parcel_multi(parcel_raw, county)
        # 2) Fall back to an address-based NC OneMap search
        if not parcel_data and address:
            parcel_data = search_address_in_nc1map(address, county)

        update_fields: dict = {}
        got_acres = bool(parcel_data and parcel_data.get("acres", 0) > 0)
        actual_county = (parcel_data or {}).get("cntyname") or county or ""
        coords_lat = (parcel_data or {}).get("latitude")
        coords_lng = (parcel_data or {}).get("longitude")
        parcel_ref = (parcel_data or {}).get("parno") or parcel_raw

        if got_acres:
            update_fields["acres"] = parcel_data["acres"]
            update_fields["acres_source"] = county_tag or "gis"
            update_fields["gis_county"] = actual_county
            update_fields["owner_name"] = parcel_data.get("owner_name")
            update_fields["land_use"] = parcel_data.get("land_use") or None
            if parcel_data.get("parno"):
                update_fields["parcel_number"] = parcel_data["parno"]
            if coords_lat and coords_lng:
                update_fields["latitude"] = coords_lat
                update_fields["longitude"] = coords_lng

        # Always build map/GIS links when an address or parcel is available, so
        # every property — from any scraper — gets both a Google Maps link and a
        # GIS viewer link.
        if address or parcel_ref:
            gmaps = build_google_maps_url(coords_lng, coords_lat, address, city, actual_county, state=state)
            if gmaps:
                update_fields["google_maps_url"] = gmaps
                update_fields["google_maps_topo_url"] = build_google_maps_topo_url(coords_lat, coords_lng, address, city, actual_county, state=state)
            gis = build_gis_url(coords_lng, coords_lat, parcel_ref, address, actual_county, state=state)
            if gis:
                update_fields["gis_url"] = gis

        if not update_fields:
            failed += 1
            logger.debug("No GIS/address data for #%s %s county=%s parcel=%s addr=%s",
                         row_id, src, county, parcel_raw[:40], address[:40])
            time.sleep(random.uniform(0.3, 0.6))
            continue

        set_parts = ", ".join(f"{k} = ?" for k in update_fields if k != "id")
        conn.execute(
            f"UPDATE properties SET {set_parts} WHERE id = ?",
            [update_fields[k] for k in update_fields if k != "id"] + [row_id],
        )
        conn.commit()
        if got_acres:
            enriched += 1
            logger.info("Enriched #%s %s %s parcel=%s -> %sac",
                        row_id, src, county, parcel_raw[:20], update_fields["acres"])
        else:
            url_updated += 1
            logger.info("Linked #%s %s %s (addr) gmaps+gis set", row_id, src, county)

        # Rate limit
        time.sleep(random.uniform(0.3, 0.6))

    conn.commit()
    conn.close()

    result = {
        "enriched": enriched,
        "url_updated": url_updated,
        "skipped_no_parcel": skipped_no_parcel,
        "failed": failed,
    }
    logger.info(f"Enrichment complete: {enriched} gis-acres, {url_updated} url-only, "
                f"{skipped_no_parcel} skipped(no data), {failed} failed")
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
    "burke": "023", "catawba": "035", "cherokee": "039",
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
        return " ".join(words)
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


# --- Cherokee County authoritative GIS (ArcGIS Open Data FeatureServer) ---
# Cherokee publishes parcels at services5.arcgis.com/.../Parcels/FeatureServer/0.
# NC OneMap mirrors the *polygon* acreage (POLY_ACRES / TOTAL_CALC), but the
# foreclosure-relevant figure is the deed/legal acreage in ``LegalLandU``,
# which the county's own GIS viewer displays. We query that field directly.
CHEROKEE_PARCELS_URL = (
    "https://services5.arcgis.com/UmQCfTNQbyTzAV5N/arcgis/rest/services/"
    "Parcels/FeatureServer/0/query"
)


def _cherokee_query(where: str, timeout: int = 15) -> Optional[list[dict]]:
    try:
        resp = curl_requests.get(
            CHEROKEE_PARCELS_URL,
            params={
                "f": "json",
                "where": where,
                "outFields": "NEWPIN,LegalLandU,TOTAL_CALC,POLY_ACRES",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultRecordCount": "10",
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.debug("Cherokee GIS HTTP %d", resp.status_code)
            return None
        data = resp.json()
        if data.get("error"):
            logger.debug("Cherokee GIS error: %s", data["error"])
            return None
        return data.get("features", [])
    except Exception as e:  # pragma: no cover - network
        logger.debug("Cherokee GIS query failed: %s", e)
        return None


def cherokee_lookup(parcel: Optional[str] = None, address: Optional[str] = None,
                    timeout: int = 15) -> Optional[dict]:
    """Look up a Cherokee parcel in the county's authoritative FeatureServer.

    Returns the deed/legal acreage (``LegalLandU``, summed across polygon
    features) plus centroid. Returns None when no parcel matches.
    """
    if not parcel and not address:
        return None
    feats = None
    if parcel:
        feats = _cherokee_query(f"NEWPIN='{parcel}'", timeout)
    if not feats:
        return None
    legal = sum((f.get("attributes", {}).get("LegalLandU") or 0) for f in feats)
    if legal <= 0:
        return None
    centroid = None
    for f in feats:
        g = f.get("geometry")
        if g and g.get("rings"):
            pts = [p for ring in g["rings"] for p in ring]
            if pts:
                centroid = (
                    round(sum(p[0] for p in pts) / len(pts), 6),
                    round(sum(p[1] for p in pts) / len(pts), 6),
                )
                break
    return {
        "parno": parcel,
        "acres": round(legal, 2),
        "address": None,
        "owner": None,
        "latitude": centroid[1] if centroid else None,
        "longitude": centroid[0] if centroid else None,
        "confidence": "high",
        "source_tag": "cherokee_gis",
    }


def county_specific_lookup(county: Optional[str], parcel: Optional[str] = None,
                           address: Optional[str] = None) -> Optional[dict]:
    """Preferred county-specific GIS lookup. Returns None to signal "fall back
    to NC OneMap". Currently supports Cherokee; extend here as
    more county services are integrated.
    """
    c = (county or "").lower().strip()
    if c == "cherokee":
        return cherokee_lookup(parcel, address)
    return None


def re_enrich_all(conn) -> dict:
    """Re-enrich the entire list, preferring authoritative county GIS.

    For each property we try ``county_specific_lookup`` (Cherokee
    today) first; only if that returns nothing do we fall back to NC OneMap.
    Manually-locked rows (``manual_acres_set`` set) are skipped so a user fix
    survives re-enrichment. Existing acres are only overwritten on a successful
    lookup, so good data is never clobbered by a transient miss.
    """
    rows = conn.execute(
        "SELECT id, source, county, parcel_number, address, city, acres_source, "
        "manual_acres_set FROM properties"
    ).fetchall()
    cherokee = 0
    nc_updated = 0
    skipped = 0
    failed = 0
    for row_id, src, county, parcel, address, city, acres_src, manual_set in rows:
        if (manual_set or "").strip():
            skipped += 1
            continue
        used_county = None
        parcel_data = county_specific_lookup(county, parcel, address)
        if parcel_data and parcel_data.get("confidence") == "high":
            used_county = parcel_data.get("source_tag") or "county_gis"
        else:
            parcel_data = None
            if parcel:
                parcel_data = _lookup_parcel_multi(parcel, county)
            if not parcel_data and address:
                parcel_data = search_address_in_nc1map(address, county)
            used_county = "gis" if parcel_data else None
        if not parcel_data:
            failed += 1
            time.sleep(random.uniform(0.3, 0.6))
            continue

        update_fields = {
            "acres": parcel_data["acres"],
            "acres_source": used_county or "gis",
            "gis_county": parcel_data.get("cntyname") or county,
        }
        if parcel_data.get("parno"):
            update_fields["parcel_number"] = parcel_data["parno"]
        if parcel_data.get("owner_name"):
            update_fields["owner_name"] = parcel_data["owner_name"]
        if parcel_data.get("land_use"):
            update_fields["land_use"] = parcel_data["land_use"]
        if parcel_data.get("latitude") and parcel_data.get("longitude"):
            update_fields["latitude"] = parcel_data["latitude"]
            update_fields["longitude"] = parcel_data["longitude"]
        gis = build_gis_url(parcel_data.get("longitude"), parcel_data.get("latitude"),
                            parcel_data.get("parno"), address, county)
        if gis:
            update_fields["gis_url"] = gis
        gmaps = build_google_maps_url(parcel_data.get("longitude"), parcel_data.get("latitude"),
                                      address, city, parcel_data.get("cntyname") or county)
        if gmaps:
            update_fields["google_maps_url"] = gmaps

        set_parts = ", ".join(f"{k} = ?" for k in update_fields)
        conn.execute(
            f"UPDATE properties SET {set_parts} WHERE id = ?",
            list(update_fields.values()) + [row_id],
        )
        conn.commit()
        if used_county == "cherokee_gis":
            cherokee += 1
        else:
            nc_updated += 1
        logger.info("Re-enriched #%s %s %s -> %sac (%s)",
                    row_id, src, county, parcel_data["acres"], used_county)
        time.sleep(random.uniform(0.3, 0.6))

    return {"cherokee": cherokee,
            "nc_onemap_updated": nc_updated, "skipped_locked": skipped,
            "failed": failed}


def reconcile_archive(conn, min_acres: Optional[float] = None) -> dict:
    """Make archive status consistent with current acreage.

    - Active properties with 0 < acres < min_acres are archived (fixes ones
      kept active by a falsely-high match).
    - Archived properties with acres >= min_acres are unarchived (fixes ones
      archived by a falsely-low match, or stale status).
    Properties with NULL acres are left untouched.
    """
    from .config import config
    from .db import archive_below_acres
    threshold = min_acres if min_acres is not None else config.MIN_ACRES
    today = date.today().isoformat()

    n_archived = archive_below_acres(conn, threshold)
    n_unarchived = conn.execute(
        "UPDATE properties SET status = 'active', last_seen = ? "
        "WHERE status = 'archived' AND acres IS NOT NULL AND acres >= ?",
        (today, threshold),
    ).rowcount
    conn.commit()
    logger.info("Reconcile: archived=%s unarchived=%s (threshold=%s)",
                n_archived, n_unarchived, threshold)
    return {"archived": n_archived, "unarchived": n_unarchived}
