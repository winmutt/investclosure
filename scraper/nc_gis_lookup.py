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

    A transport failure is retried once with a longer timeout. The service
    answers a typical query in 2-3 s but regularly takes longer, and every
    caller treats ``None`` as "this pattern found nothing" and moves to the
    next one — so one slow response silently throws away the MOST specific
    LIKE pattern of an address search (measured live: the first query of both
    a Buncombe and a Swain lookup came back ``None`` at timeout=5). An empty
    result stays empty; only a failed request is retried.
    """
    data = dict(post_data)
    data["returnGeometry"] = "true"
    data["outSR"] = "4326"
    # nconemap_net falls back to an unverified connection while the state's
    # TLS certificate is expired (2026-11-05) instead of silently losing
    # every parcel/acreage lookup.
    from scraper import nconemap_net

    for attempt, budget in enumerate((timeout, max(int(timeout), 10))):
        try:
            resp = nconemap_net.post(
                NC_ONEMAP_URL,
                data=data,
                timeout=budget,
            )
            if resp.status_code != 200:
                logger.debug("NC1Map HTTP %d", resp.status_code)
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
            if payload.get("error"):
                logger.debug("NC1Map error: %s", payload["error"])
                return None
            feats = payload.get("features", [])
            return [f for f in feats if f.get("attributes")] if feats else []
        except Exception as e:  # noqa: BLE001 - transient, retried once
            logger.debug("NC1Map query failed (attempt %d): %s", attempt + 1, e)
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

    lu_code = a.get("parusecd2") or a.get("parusecode")
    lu_desc = a.get("parusedsc2") or a.get("parusedesc")
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

        fips = NC_COUNTY_FIPS.get((county or "").lower().strip())

        # Try each normalization variant (raw, dash-stripped, and — for
        # McDowell-style notices — middle-segment zero-padded) so callers
        # can pass the notice-printed parcel verbatim.
        for clean in _parcel_variants(parcel):
            # Strategy 1: Try nparno = "37" + fips + "_" + parcel
            if fips:
                nparno = f"37{fips}_{clean}"
                feats = _nc1map_query({
                    "where": f"cntyfips='{fips}' AND nparno='{nparno}'",
                    "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
                    "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
                    "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
    # McDowell County notices print tax parcels as NNNN-NN-NNNN
    # ("0668-15-3806") but NC OneMap stores 4-4-4 grouped 12-digit PINs
    # ("066800153806") — pad the middle segment to 4 digits (tried last,
    # after the exact/dash-stripped forms, so counties whose de-dashed form
    # matches directly are never affected).
    m = re.match(r"^(\d{4})-(\d{1,3})-(\d{4})$", cleaned)
    if m:
        padded = m.group(1) + m.group(2).zfill(4) + m.group(3)
        if padded not in variants:
            variants.append(padded)
        # Buncombe County notices print just the PARENT PIN in the same
        # NNNN-NN-NNNN shape ("9634-43-5464") while NC OneMap stores the
        # 15-digit form with a 5-digit sub-parcel suffix ("963443546400000")
        # — so also try the de-dashed digits plus a 5-zero sub-parcel suffix
        # (added last; every earlier variant still fails the lookup first,
        # and any match remains county-filtered). Digits already ending in
        # '0000' are left alone — suffixing more zeros could form a
        # different parcel.
        digits = m.group(1) + m.group(2) + m.group(3)
        if not digits.endswith("0000"):
            trailing = digits + "00000"
            if trailing not in variants:
                variants.append(trailing)
    # The mirror image: a PIN stored WITHOUT the dashes its county roll keeps.
    # Jackson holds '7564-51-5920' in OneMap while the source page printed
    # '7564515920' (#6052, acres NULL forever — de-dashing can never add the
    # dashes back). Re-dash a bare digit string in the county spellings this
    # table is known to use: 4-2-4 (Jackson) and 4-2-4-5 (Buncombe). Appended
    # last, and every query stays county-filtered, so an exact-string match on
    # a re-dashed PIN is still the parcel it names.
    bare = re.fullmatch(r"\d+", cleaned) and cleaned
    if bare and len(bare) in (10, 15):
        dashed = (f"{bare[:4]}-{bare[4:6]}-{bare[6:]}" if len(bare) == 10
                  else f"{bare[:4]}-{bare[4:6]}-{bare[6:10]}-{bare[10:]}")
        if dashed not in variants:
            variants.append(dashed)
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
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
                "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
            "outFields": "parno,altparno,nparno,cntyfips,cntyname,gisacres,recareano,siteadd,ownname,parusecd2,parusedsc2",
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
    # same-origin viewer/proxy. Only parcel- or coordinate-backed links
    # "take you to the property" — anything less returns None so callers
    # render no GIS button instead of a generic map. (Google Maps links
    # cover address-only rows.)
    if s in ("NC", ""):
        if parcel or (lng is not None and lat is not None):
            return get_nconemap_viewer_url(lng, lat, parcel, county)
        return None

    # Any other state (SC/AL/...): no parcel service here — return None
    # rather than a wrong-state viewer link.
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

    Address- or coordinate-backed only (link-depth policy): a county-only
    query ("Clay NC") doesn't take you to the property, so it returns None
    instead of a generic map search.
    """
    st = (state or "NC").strip() or "NC"
    if address and address.strip():
        parts = [p for p in [address, city, county, st] if p and p.strip()]
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


def enrich_properties(source: Optional[str] = None,
                      status: str = "active",
                      min_id: Optional[int] = None,
                      ids: Optional[list] = None,
                      backlog: Optional[int] = None) -> dict:
    """Enrich properties in DB with GIS data.

    Queries NC OneMap for all properties that have a parcel_number but no GIS acres.
    Upserts the enriched data back into the properties table.

    Args:
        source: Optional source filter (e.g., "kania_law"). Defaults to all.
        status: Row status to process (default "active" — archived rows are
            skipped to avoid burning API calls; use "all" for every row).
        min_id: Optional "only rows inserted after this id" filter. Each
            lookup costs ~10s, so a scraper that just inserted thousands of
            rows (zillow after pagination) must not replay the whole
            backlog every run — pass the highest id from before the
            insert to enrich only this run's new rows.
        backlog: Optional count of OLDER NC rows (id <= min_id) to retry in
            the same pass, oldest first. Without it a failed lookup is
            permanent even when the reason for the failure is later fixed.

    Returns:
        dict with counts: enriched, skipped_no_parcel, skipped_already_gis,
        failed, backlog
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
    if status and status != "all":
        where += " AND status = ?"
        params.append(status)

    # The backlog query needs the same filters WITHOUT the id window, so the
    # scope is captured here and the window is appended separately.
    base_where, base_params = where, list(params)
    columns = ("id, source, county, parcel_number, address, city, state, "
               "acres_source, gis_url, google_maps_url, acres")

    if min_id is not None:
        where += " AND id > ?"
        params.append(int(min_id))
    if ids:
        # Operator escape hatch: re-enrich a named set of rows (verifying a
        # multi-parcel fix on #2739 without replaying the backlog).
        id_list = [int(i) for i in ids]
        where += f" AND id IN ({','.join('?' * len(id_list))})"
        params.extend(id_list)

    # Get properties (with a parcel OR an address) that need GIS enrichment
    rows = list(conn.execute(
        f"SELECT {columns} FROM properties WHERE {where}",
        params,
    ).fetchall())

    # Drain a slice of the older backlog too. A lookup that fails once used to
    # be permanent: this hook is scoped to ``id > min_id`` (a 4,000-row run may
    # not replay history), so a row that failed for a reason we later FIXED —
    # a new PIN variant, a case-folded LIKE, a county that started publishing
    # recareano — stayed NULL forever (#6084, #5657 resolved on a direct call
    # while the auto pass left them alone). Oldest first, NC only (OneMap
    # covers no other state), and bounded: a slow night of retries must not
    # turn a scrape run into an all-day GIS crawl.
    backlog_rows: list = []
    if backlog and min_id is not None and not ids:
        backlog_rows = list(conn.execute(
            f"SELECT {columns} FROM properties WHERE {base_where}"
            " AND id <= ?"
            " AND (state IS NULL OR TRIM(state) = '' OR UPPER(state) = 'NC')"
            " ORDER BY id ASC LIMIT ?",
            base_params + [int(min_id), int(backlog)],
        ).fetchall())
        fresh_ids = {r[0] for r in rows}
        backlog_rows = [r for r in backlog_rows if r[0] not in fresh_ids]
        if backlog_rows:
            logger.info("Retrying %s older NC row(s) that still need "
                        "enrichment", len(backlog_rows))
    rows.extend(backlog_rows)

    if not rows:
        logger.info("No properties need GIS enrichment")
        conn.close()
        return {"enriched": 0, "url_updated": 0, "skipped_no_parcel": 0,
                "failed": 0, "backlog": len(backlog_rows)}

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
         acres_src, gis_url, gmaps_url, row_acres) = row

        # NC OneMap only covers North Carolina — never touch other states'
        # rows (a past run overwrote TN TPAD deep links with generic URLs).
        if ((state or "").strip().upper() not in ("NC", "")):
            logger.debug("Skipping non-NC row #%s %s state=%s", row_id, src, state)
            continue

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
            if has_house_number(address):
                parcel_data = search_address_in_nc1map(address, county)
            else:
                # No situs number to anchor on (placeholder "0 Leon Barron Rd"
                # or a bare street name): a loose LIKE would bind a neighbour,
                # so first require an owner group whose summed acreage matches
                # the listing (26.02ac = 23.0 + 3.02, Jackson #2739), then fall
                # back to the plain street search (token stripped) as before.
                parcel_data = search_owner_parcels_in_nc1map(
                    address, county, target_acres=row_acres) \
                    or search_address_in_nc1map(address, county)

        update_fields: dict = {}
        got_acres = bool(parcel_data and parcel_data.get("acres", 0) > 0)
        actual_county = (parcel_data or {}).get("cntyname") or county or ""
        coords_lat = (parcel_data or {}).get("latitude")
        coords_lng = (parcel_data or {}).get("longitude")
        parcel_ref = (parcel_data or {}).get("parno") or parcel_raw
        # Tract spanning several PINs (owner-group match): the viewer link and
        # the DB list all of them, ``parcel_number`` keeps the primary.
        multi_pins = [p["parno"] for p in (parcel_data or {}).get("parcels") or []
                      if p.get("parno")]
        if len(multi_pins) > 1:
            parcel_ref = ",".join(multi_pins)

        if got_acres:
            update_fields["acres"] = parcel_data["acres"]
            update_fields["acres_source"] = county_tag or "gis"
            update_fields["gis_county"] = actual_county
            update_fields["owner_name"] = parcel_data.get("owner_name")
            update_fields["land_use"] = parcel_data.get("land_use") or None
            if parcel_data.get("parno"):
                update_fields["parcel_number"] = parcel_data["parno"]
            if len(multi_pins) > 1:
                update_fields["parcel_numbers"] = ",".join(multi_pins)
            if coords_lat and coords_lng:
                update_fields["latitude"] = coords_lat
                update_fields["longitude"] = coords_lng

        # Parcel-to-address backfill: a row whose notice carried no street
        # address gets the assessor's site address so Maps links resolve to
        # the property. Runs even without acres (site data may exist while
        # the acreage field is empty) but never overwrites a parsed address.
        if not address and parcel_data and parcel_data.get("site_address"):
            address = parcel_data["site_address"]
            update_fields["address"] = address

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
            # A row inserted with unknown acreage can resolve BELOW the
            # threshold here (#1107); archive immediately so it never lingers
            # active awaiting a full-suite archive pass. Manual overrides
            # are left alone.
            manual = conn.execute(
                "SELECT manual_acres_set FROM properties WHERE id = ?",
                (row_id,),
            ).fetchone()
            if (update_fields["acres"] < config.MIN_ACRES
                    and not ((manual[0] if manual else "") or "").strip()):
                from .db import archive_property
                archive_property(conn, row_id, reason="below_min_acres",
                                 archived_by="system")
                logger.info("Archived #%s: %sac < MIN_ACRES %s",
                            row_id, update_fields["acres"], config.MIN_ACRES)
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
        "backlog": len(backlog_rows),
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


# Zillow renders vacant land whose parcel carries no assessor situs number as
# "0 Leon Barron Rd" / "TBD Hart Rd" / "LOT 6 Chestnut Walk Dr" / "NULLD
# Chestnut Walk Dr" (live token census 2026-11-09, active NC rows: 0 x65,
# TBD x27, LOT x6, 00 x3, 000, 000A, 000A, NULLD). None of those is a house
# number, and leaving the token in the LIKE pattern makes the house-number
# guard reject every real parcel on the road — #2739 "0 Leon Barron Rd" never
# enriched because OneMap stores that road's situs addresses with no number.
_HOUSE_NUMBER_RE = re.compile(
    r"^\s*(\d+[A-Za-z]?)(?:\s*-\s*\d+[A-Za-z]?)?\s+")
# Zillow fills a missing situs number with zeros or nines — live sample:
# "0 Hal Dr", "00 US 19 Highway", "000A …", "9999 Us Highway 441",
# "99999 Morgan Hill Rd", "999999 Harwood Rd". Anchoring a LIKE on those
# rejects every real parcel on the road, so they are not house numbers.
_FAKE_HOUSE_RE = re.compile(r"^(?:0+[A-Z]?|9{4,}[A-Z]?)$", re.I)
# Trailing address junk that no assessor situs address carries. The state
# token is deliberately NC-only (this is the NC OneMap path): a bare
# two-letter rule would eat the "Rd" of "Old Mill Rd".
_ZIP_SUFFIX_RE = re.compile(r"[,\s]+(?:N\.?C\.?\s+)?\d{5}(?:-\d{4})?\s*$", re.I)
_TRAILING_STATE_RE = re.compile(r"[,\s]+N\.?C\.?[.\s]*$|,\s*North Carolina\s*$",
                                re.I)
_UNIT_SUFFIX_RE = re.compile(
    r"[,\s]+(?:#|unit|apt|ste|lot|tract|spc|bldg|no)\.?\s*"
    r"[0-9][0-9A-Za-z/-]*\s*$", re.I)
_PLACEHOLDER_PREFIX_RE = re.compile(
    r"^\s*(?:(?:0+|9{4,})[A-Z]?|tbd|tba|tb[dc]|null\w*|none|unassigned"
    r"|lot|lots|tract)(?=[.\s]|$)[.\s]+"
    r"(?:\d+(?=[.\s]|$)[.\s]+)?", re.I)


def _drop_trailing_junk(text: str) -> str:
    """Strip a trailing ZIP / state / unit off a street fragment."""
    prev = None
    while text and text != prev:
        prev = text
        text = _ZIP_SUFFIX_RE.sub("", text)
        text = _TRAILING_STATE_RE.sub("", text)
        text = _UNIT_SUFFIX_RE.sub("", text)
    return text.strip(" ,")


def street_only(address: Optional[str]) -> str:
    """Return the street part of an address, without city / state / ZIP.

    ``_normalize_address`` keeps the LAST three words, so a whole listing line
    ("0 Leon Barron Rd, Sylva, NC 28779") searched OneMap for
    ``SYLVA, NC 28779`` and matched nothing — the reason most NC rows never
    enriched from their address (232 of the 241 active NC addresses are in
    this form). Unit/lot suffixes are dropped for the same reason: the LIKE
    pattern "RIVERVIEW RD #64" matches no assessor situs address.
    """
    text = (address or "").strip()
    if not text:
        return ""
    # "…, City, ST ZIP" — the street is everything before the first comma.
    head = text.split(",")[0].strip()
    head = _drop_trailing_junk(head)
    if head:
        return head
    # No comma at all ("Little Glade Creek Rd NC 28743"): peel the tail.
    return _drop_trailing_junk(text)


def has_house_number(address: Optional[str]) -> bool:
    """True when the address carries a REAL assessor house number.

    ``123``, ``123A`` and ``123-125`` count. Placeholders do not: Zillow's
    zero/nine fill-ins (``0``, ``000A``, ``99999``), ``TBD``, ``LOT 6``,
    ``NULLD``, and street-name-first addresses ("W Emory Rd",
    "Tusquittee Mountain Rd", "5.93 Indian Creek Rd"). Anchoring a LIKE on a
    fake number rejects every real parcel on the road, exactly like a missing
    one — both cases must go through the acreage-guarded owner-group resolver.
    """
    m = _HOUSE_NUMBER_RE.match(address or "")
    if not m:
        return False
    return not _FAKE_HOUSE_RE.match(m.group(1))


def strip_placeholder_house(address: Optional[str]) -> str:
    """Drop a placeholder house token so it cannot poison a LIKE pattern.

    ``"LOT 6 Chestnut Walk Dr"`` -> ``"Chestnut Walk Dr"``: the 6 is a lot
    number, not a situs number, so it goes with the token.
    """
    return _PLACEHOLDER_PREFIX_RE.sub("", address or "").strip()


def _gap_variant(pattern: str) -> Optional[str]:
    """Space-agnostic LIKE copy of a street pattern, or None.

    OneMap situs addresses carry padded spacing — "2820   LAUREL PARK  HWY",
    "210   MOUNTAIN VISTA   RD" — which an exact single-space LIKE can never
    match. Joining the tokens with ``%`` matches any run of spaces (or words)
    between them; the caller's house-number guard still rejects a wrong-house
    hit, so this loosens only the query, never the acceptance rule.
    """
    tokens = [t for t in (pattern or "").split() if t]
    if len(tokens) < 2:
        return None
    return "%".join(tokens)


def _like_patterns(street: str, house_num: Optional[str] = None) -> list[str]:
    """Ordered LIKE patterns for a street, most specific first.

    Two measured properties of the NC OneMap situs field drive this:

    * ``LIKE`` is CASE-SENSITIVE on that service — ``siteadd LIKE
      '%pigeon%creek%'`` returns 0 rows while ``'%PIGEON%CREEK%'`` returns 3.
      ``street_canon`` hands back lowercase abbreviations, so every pattern
      built from it was dead code. Everything is upper-cased here.
    * values are padded AND abbreviated at once ("177   PIGEON CREEK TRL"),
      so the space-agnostic ``%``-join has to be built from EACH spelling
      variant: the padded form of the un-abbreviated phrase still cannot
      match a roll that abbreviates (row #5658, "177 Pigeon Creek Trail").

    Priority (still bounded — the address fallback never searches deeper than
    the first TWO street words):
      1. house number + full pattern and each spelling variant, gap form
         included (a gap form is a superset: ``%`` matches a single space too),
      2. the same patterns unanchored,
      3. house number + first street word,
      4. canon-joined first TWO street words (wildcard between).
    """
    from .street_canon import canon_tokens, pattern_variants

    street = " ".join((street or "").split()).upper()
    patterns: list[str] = []

    def add(pattern: Optional[str]) -> None:
        # Upper-case, not as-written: the rolls store situs addresses in
        # upper case and the service's LIKE is case-sensitive (see above), so
        # a lowercase pattern — which is what canon_tokens() returns — finds
        # nothing at all.
        pattern = (pattern or "").strip().upper()
        if pattern and pattern not in patterns:
            patterns.append(pattern)

    variants = ([street, *pattern_variants(street, limit=4)[1:]] if street
                else [])
    if house_num:
        for variant in variants:
            add(f"{house_num} {variant}")
            add(_gap_variant(f"{house_num} {variant}"))
    for variant in variants:
        add(variant)
        add(_gap_variant(variant))
    first_word = street.split()[0] if street.split() else ""
    if house_num and first_word:
        add(f"{house_num} {first_word}")
    if street:
        two_tokens = "%".join(canon_tokens(street)[:2])
        # House-anchored version of the loosest tier, and gap-joined from the
        # house number out: Swain's roll stores "177   PIGEON CREEK TRL",
        # where neither "177 PIGEON" (padded space) nor an abbreviated street
        # (the roll abbreviates the TYPE, which street_canon does not map)
        # can reach it — "177%PIGEON%CREEK" does, and the house number still
        # anchors the match (#5658, 177 Pigeon Creek Trail).
        if house_num and two_tokens:
            add(f"{house_num}%{two_tokens}")
        add(two_tokens)
    return patterns


def search_address_in_nc1map(address: str, county: str) -> Optional[dict]:
    """Try to find a parcel by normalized street address in NC OneMap.

    The LIKE search normalizes the house number away and can bind a
    neighboring parcel on the same road (#250 Bell Hill Rd bound to #1546,
    #255 Coweeta Lake Cir to 901). When the input carries a house number it
    must survive on the returned siteadd; house-anchored LIKE queries run
    first, and a wrong-house loose hit is rejected (parcel-deep-or-nothing).
    """
    if not address or not county:
        return None

    # A placeholder token ("0", "TBD", "LOT 6") is not a house number, and it
    # must not poison the LIKE pattern either — strip it from the search copy.
    # City / state / ZIP go first: they are the tail the normalizer keeps.
    address = street_only(address) or address
    if not has_house_number(address):
        address = strip_placeholder_house(address)
    normalized = _normalize_address(address)
    if not normalized:
        return None

    fips = _COUNTY_FIPS.get((county or "").lower().strip())
    if not fips:
        return None

    m = re.match(r"^\s*(\d+)\s+", address)
    house_num = m.group(1) if m else None

    def _first_valid(feats: Optional[list[dict]]) -> Optional[dict]:
        for one in feats or []:
            result = _clean_features([one])
            if not result or result.get("acres", 0) <= 0:
                continue
            site = (result.get("site_address") or "").strip()
            # OneMap zero-pads siteadd house numbers to a fixed width
            # ("000454 WOODSONG DR") — allow leading zeros before the match.
            if house_num and not re.match(rf"^0*{house_num}(?![0-9])", site):
                logger.info(
                    "Address search rejected wrong-house match %s site=%r for %r (%s)",
                    result.get("parno"), site, address, county,
                )
                continue
            return result
        return None

    # Query with wildcards between words
    like_parts = " ".join(normalized.split())

    # Patterns (case-folded, padded-spacing-aware) come from _like_patterns:
    # house-anchored full pattern and spelling variants first, then the same
    # unanchored, then house number + first street word, then the canon-joined
    # first TWO street words. Address fallback never searches deeper than two
    # street words; acceptance is still guarded by the house number below.
    patterns = _like_patterns(like_parts, house_num)
    for pattern in patterns:
        if not pattern:
            continue
        where_str = f"cntyfips='{fips}' AND siteadd LIKE '%{pattern}%'"
        resp = _nc1map_query(
            {
                "where": where_str,
                # recareano is the acreage Buncombe County publishes (its
                # gisacres is 0.0 on every parcel) — without it a correct hit
                # was thrown away by _clean_features and the row kept NULL.
                "outFields": ("parno,altparno,nparno,cntyname,cntyfips,"
                              "gisacres,recareano,siteadd,ownname,"
                              "parusecd2,parusedsc2"),
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": "10",
            },
            timeout=5,
        )
        result = _first_valid(resp)
        if result:
            return result

    return None


def search_owner_parcels_in_nc1map(address: str, county: str,
                                   target_acres: Optional[float],
                                   tolerance: float = 0.12
                                   ) -> Optional[dict]:
    """Resolve a placeholder-address tract to the OWNERSHIP GROUP it spans.

    County rolls split one large tract ("0 Leon Barron Rd", 26.02ac listed)
    into several PINs sharing a site address and owner — Jackson live case:
    7528-56-9321 (23.0ac) + 7528-56-9996 (3.02ac), both GUFFEY, exactly
    26.02ac. This groups every parcel found on the street by owner and picks
    the group whose **total acreage** matches the listing's ``target_acres``
    within ``tolerance`` — the acreage guard is what keeps a loose LIKE from
    binding a neighbour's lot. No group matches -> None: no parcel beats a
    WRONG parcel (link-depth policy).

    Returns a ``_clean_features``-shaped dict for the group's largest parcel
    (its centroid centers the viewer) plus ``parcels``:
    ``[{"parno", "acres"}, …]`` sorted primary-first.
    """
    if not address or not county or not target_acres or target_acres <= 0:
        return None
    street = strip_placeholder_house(street_only(address))
    normalized = _normalize_address(street)
    if not normalized:
        return None
    fips = _COUNTY_FIPS.get((county or "").lower().strip())
    if not fips:
        return None

    like_parts = " ".join(normalized.split())
    # Same case-folded, padded-spacing-aware pattern ladder as the address
    # search (the old list was lowercase on its canon tier and never built a
    # gap form for the abbreviated variants, so both tiers were dead).
    patterns = _like_patterns(like_parts)

    seen: dict[str, dict] = {}
    for pattern in patterns:
        if not pattern:
            continue
        feats = _nc1map_query({
            "where": f"cntyfips='{fips}' AND siteadd LIKE '%{pattern}%'",
            "outFields": ("parno,altparno,cntyname,cntyfips,gisacres,"
                          "recareano,siteadd,ownname,parusecd2,parusedsc2"),
            "returnGeometry": "false",   # _nc1map_query forces geometry on
            "f": "json",
            "resultRecordCount": "100",
        }, timeout=8)
        for one in feats or []:
            parno = ((one.get("attributes") or {}).get("parno") or "").strip()
            if parno and parno not in seen:
                seen[parno] = one
        if seen:
            break          # exact street phrase is the precise query

    if not seen:
        return None

    best = None
    groups: dict[str, list[dict]] = {}
    for one in seen.values():
        a = one.get("attributes") or {}
        owner = re.sub(r"\s+", " ", str(a.get("ownname") or "")).strip().upper()
        cleaned = _clean_features([one])
        if not owner or cleaned is None:
            continue
        groups.setdefault(owner, []).append(cleaned)
    for owner, clean in groups.items():
        total = round(sum(c["acres"] for c in clean), 2)
        if abs(total - target_acres) > target_acres * tolerance:
            continue
        score = abs(total - target_acres)
        if best is None or score < best[0]:
            best = (score, total, clean)
    if best is None:
        return None
    _score, total, clean = best
    primary = max(clean, key=lambda c: c["acres"])
    result = dict(primary)
    result["acres"] = total
    result["parcels"] = [
        {"parno": c["parno"], "acres": c["acres"]}
        for c in sorted(clean, key=lambda c: -c["acres"]) if c.get("parno")
    ]
    return result


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


def fill_acres_in_memory(props: "list[dict]", rate: float = 0.6) -> dict:
    """Fill missing acreage (and coords/address) on in-memory property dicts
    via the NC OneMap parcel service — the same lookup the post-insert
    enricher runs, but BEFORE insert so sub-threshold rows never reach the
    DB or Telegram (the county pages publish no acreage themselves).

    Only rows with state NC (or unset), acres still None, and a
    parcel_number are looked up. Rows whose acres stay unknown are left
    alone (keep-unknown policy; the post-insert enricher may retry them).
    Returns {"enriched": n, "failed": m}.
    """
    svc = NC1MapService()
    enriched = failed = 0
    for p in props:
        if p.get("acres") is not None:
            continue
        if (p.get("state") or "NC").upper() != "NC":
            continue
        parcel = p.get("parcel_number")
        if not parcel:
            continue
        # NC OneMap stores many counties' parcels WITHOUT separators
        # (McDowell "173900312535"); page-sourced parcels often carry dashes
        # ("1739-00-31-2535"). Try the raw form, then the de-dashed form.
        variants = [str(parcel).strip()]
        if "-" in variants[0]:
            v = variants[0].replace("-", "")
            if v not in variants:
                variants.append(v)
        data = None
        for cand in variants:
            try:
                data = svc.by_parcel(cand, county=p.get("county"))
            except Exception as e:
                logger.warning("in-memory parcel lookup failed %s: %s", cand, e)
                data = None
            if data and data.get("acres"):
                break
        if data and data.get("acres"):
            p["acres"] = data["acres"]
            p["acres_source"] = "gis"
            if data.get("latitude") and p.get("latitude") is None:
                p["latitude"] = data["latitude"]
            if data.get("longitude") and p.get("longitude") is None:
                p["longitude"] = data["longitude"]
            if not p.get("address"):
                addr = data.get("siteadd") or data.get("site_address")
                if addr:
                    p["address"] = addr
            # Rebuild map links that needed the now-filled address/coords.
            if not p.get("google_maps_url") and p.get("address"):
                p["google_maps_url"] = build_google_maps_url(
                    p.get("longitude"), p.get("latitude"), p.get("address"),
                    None, p.get("county"), state="NC")
            p["gis_url"] = p.get("gis_url") or build_gis_url(
                p.get("longitude"), p.get("latitude"),
                p.get("parcel_number"), p.get("address"),
                p.get("county"), state="NC")
            enriched += 1
        else:
            failed += 1
        time.sleep(rate)
    return {"enriched": enriched, "failed": failed}
