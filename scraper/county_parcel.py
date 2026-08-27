"""Resolve county tax parcel IDs to NC statewide PINs via each county's own
ArcGIS parcel service.

Why this exists
---------------
NC OneMap (used for GIS enrichment) only indexes the 10-12 digit *statewide*
PIN (``parno``). Foreclosure notices instead print the county's own *tax*
parcel ID (Madison REID ``27011``, Burke REID ``8746``, etc.), which NC OneMap
cannot match. The old enrichment code fell back to an **address** search, which
silently bound the property to the *first* parcel on that street — frequently
the wrong one (e.g. Madison 27011 was attached to an unrelated 53-ac lot).

Each county publishes its own parcel Feature Service mapping tax-id -> PIN, so
we resolve there first and only fall back to the address search when no county
service is configured.

Registry entries are added per county as their service + field mapping are
validated. Each entry:
    url:       Feature Service layer URL
    tax_field: field holding the county tax parcel ID
    pin_field: field holding the NC statewide PIN (usable in NC OneMap)
"""
from __future__ import annotations
import logging
import re
import ssl
from typing import Optional, Any
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# County parcel services (validated). Keyed by lower-case county name.
COUNTY_PARCEL_SERVICES: dict[str, dict[str, str]] = {
    "madison": {
        "url": "https://services3.arcgis.com/NwIC4HArqo0JlKGT/arcgis/rest/services/2025_Parcels/FeatureServer/19",
        "tax_field": "REID",
        "pin_field": "PIN",
    },
    "burke": {
        "url": "https://services3.arcgis.com/axQ4OCSpcxALIQsV/arcgis/rest/services/Tax_Parcels/FeatureServer/0",
        "tax_field": "REID",
        "pin_field": "PIN",
    },
}

# County tax IDs we know are NOT 10-12 digit statewide PINs (used to decide
# whether county-service resolution is worth attempting).
_TAX_ID_HINT = re.compile(r"[^0-9]")


def _ctx() -> ssl.SSLContext:
    # Some county ArcGIS servers present cert chains the container doesn't
    # trust; the data is public, so verify-mode is relaxed for these GETs.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _query(service_url: str, tax_field: str, tax_id: str) -> Optional[dict]:
    """Query a county parcel service for one tax id; return attribute dict."""
    where = f"{tax_field}='{tax_id}'"
    url = (
        f"{service_url}/query?where={quote(where)}"
        f"&outFields=*&returnGeometry=false&f=json&resultRecordCount=1"
    )
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urlopen(req, timeout=25, context=_ctx()).read().decode("utf-8", "ignore")
        import json
        j = json.loads(data)
        feats = j.get("features") or []
        if not feats:
            return None
        return feats[0].get("attributes") or None
    except Exception as e:
        logger.debug("county parcel query failed for %s=%s: %s", tax_field, tax_id, e)
        return None


def resolve_county_tax_id(county: str, tax_id: str) -> Optional[dict]:
    """Resolve a county tax parcel ID to its NC statewide PIN.

    Args:
        county: County name (case-insensitive)
        tax_id: The county tax parcel ID from the foreclosure notice

    Returns:
        dict with keys: pin, owner, address, acres, raw — or None if the county
        has no configured service or the tax id was not found.
    """
    if not county or not tax_id:
        return None
    cfg = COUNTY_PARCEL_SERVICES.get(county.strip().lower())
    if not cfg:
        return None

    tax_field = cfg["tax_field"]
    pin_field = cfg["pin_field"]

    attrs = None
    # Try the raw tax id, then a digits-only normalization (handles formats
    # like "06-244-5-24" vs "06244524").
    for variant in (tax_id.strip(), re.sub(r"[^0-9]", "", tax_id.strip())):
        if not variant:
            continue
        attrs = _query(cfg["url"], tax_field, variant)
        if attrs:
            break
    if not attrs:
        return None

    pin = attrs.get(pin_field)
    if pin is None:
        return None
    pin = str(pin).strip()
    if not pin:
        return None

    return {
        "pin": pin,
        "owner": (attrs.get("PROPERTY_OWNER") or attrs.get("OWNER")
                  or attrs.get("Name1") or "").strip() or None,
        "address": (attrs.get("LOCATION_ADDR") or attrs.get("ADDRESS1")
                    or attrs.get("PHYSICAL_LOCATION") or "").strip() or None,
        "acres": attrs.get("ACREAGE") or attrs.get("StatedArea") or None,
        "raw": attrs,
    }
