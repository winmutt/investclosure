"""Enrich Knox County TN properties via KGIS parcel services.

Knox County (Knoxville) parcels are absent from TNMap's statewide CADASTRAL
layer (verified: COUNT=0 for Knox/Hamilton/Davidson/Shelby), so TNMap
enrichment can never match them. KGIS exposes a queryable ArcGIS backend
through its same-origin proxy::

    https://www.kgis.org/proxy/proxy.ashx?
        https://www.kgis.org/arcgis/rest/services/Maps/QueryTasks/MapServer

Layer 3 (Parcels) carries PARCELID / OWNER / FULL_ADDRESS / SHAPE.AREA
(square feet). Parcel-deep viewer links use the KGIS Maps parcel parameter::

    https://www.kgis.org/kgismaps/Map.htm?parcel=<PARCELID>

Run inside the container:
    python3 -m scraper.kgis_enrich [--limit N]
"""
from __future__ import annotations
import logging
import random
import re
import time
import urllib.parse
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_PX = "https://www.kgis.org/proxy/proxy.ashx?"
_SVC = "https://www.kgis.org/arcgis/rest/services/Maps/QueryTasks/MapServer"
_PARCELS = 3
_FIELDS = "PARCELID,OWNER,FULL_ADDRESS,CLTMAP,PARCEL_GROUP,SHAPE.AREA"

_HOUSE_NUM_RE = re.compile(r"^\s*(\d+)\s+(.+)$")


def _parcel_variants(parcel: str) -> List[str]:
    """KGIS PARCELIDs carry no spaces (082NK024); notices print spaces
    (103J D 009) or dash fragments. Try raw, spaceless, and dashless."""
    out = []
    for v in (parcel, parcel.replace(" ", ""), parcel.replace("-", "")):
        v = (v or "").strip().upper()
        if v and v not in out:
            out.append(v)
    return out


def _split_address(address: str) -> Optional[tuple[str, str]]:
    """Return (house_number, street_core) or None.

    Street core drops directionals/suffixes so 'BIDDLE ST' matches KGIS
    'BIDDLE ST' regardless of notice abbreviations.
    """
    m = _HOUSE_NUM_RE.match(address or "")
    if not m:
        return None
    num, rest = m.group(1), m.group(2).upper()
    toks = re.sub(r"[^A-Z0-9 ]", " ", rest).split()
    stop = {"ST", "STREET", "AVE", "AVENUE", "DR", "DRIVE", "RD", "ROAD",
            "LN", "LANE", "CT", "COURT", "CIR", "CIRCLE", "BLVD",
            "BOULEVARD", "HWY", "HIGHWAY", "PIKE", "WAY", "TRAIL",
            "N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST"}
    core = " ".join(t for t in toks if t not in stop)
    if not core:
        return None
    return num, core


def _query(page, where: str) -> list:
    url = (_PX + _SVC + f"/{_PARCELS}/query?" + urllib.parse.urlencode({
        "f": "json", "where": where, "outFields": _FIELDS,
        "returnGeometry": "false", "resultRecordCount": "10",
    }))
    js = ("([x]) => fetch(x).then(r => r.json()).then(j => "
          "JSON.stringify((j.features || []).map(f => f.attributes)))"
          ".catch(e => 'ERR:' + String(e))")
    import json as _json
    try:
        raw = page.evaluate(js, [url])
    except Exception as e:
        logger.warning("KGIS query failed: %s", e)
        return []
    if not isinstance(raw, str) or raw.startswith("ERR:"):
        logger.warning("KGIS query error: %s", (raw or "")[:150])
        return []
    try:
        feats = _json.loads(raw)
    except Exception:
        return []
    return feats if isinstance(feats, list) else []


def _match(prop: dict, feats: list) -> Optional[dict]:
    """First feature whose FULL_ADDRESS carries the house number and shares
    a street token with the notice address (mirrors tnmap strictness)."""
    split = _split_address(prop.get("address") or "")
    num = split[0] if split else None
    want = set(split[1].split()) if split else set()
    for f in feats:
        full = str(f.get("FULL_ADDRESS") or "").upper()
        if num and not re.search(rf"(^|\s){re.escape(num)}(\s|$)", full):
            continue
        if want and not (want & set(re.sub(r"[^A-Z0-9 ]", " ", full).split())):
            continue
        try:
            area = float(f.get("SHAPE.AREA") or 0)
        except (TypeError, ValueError):
            area = 0
        return {
            "parcel_id": (f.get("PARCELID") or "").strip(),
            "owner": (f.get("OWNER") or "").strip(),
            "site_address": (f.get("FULL_ADDRESS") or "").strip(),
            "acres": round(area / 43560, 2) if area > 0 else None,
        }
    return None


def _feature_to_match(f: dict) -> dict:
    try:
        area = float(f.get("SHAPE.AREA") or 0)
    except (TypeError, ValueError):
        area = 0
    return {
        "parcel_id": (f.get("PARCELID") or "").strip(),
        "owner": (f.get("OWNER") or "").strip(),
        "site_address": (f.get("FULL_ADDRESS") or "").strip(),
        "acres": round(area / 43560, 2) if area > 0 else None,
    }


def lookup(page, address: str, parcel: str = "") -> Optional[dict]:
    """Parcel-exact first, then address LIKE. Returns match dict or None."""
    for v in _parcel_variants(parcel or ""):
        feats = _query(page, f"PARCELID = '{v}'")
        time.sleep(random.uniform(0.3, 0.6))
        if feats:
            return _feature_to_match(feats[0])
    split = _split_address(address or "")
    if split:
        num, core = split
        tok = core.split()[0]
        # KGIS stores number-LAST ("BIDDLE ST 1123"); try both orders.
        for pat in (f"FULL_ADDRESS LIKE '%{num}%{tok}%'",
                    f"FULL_ADDRESS LIKE '%{tok}%{num}%'"):
            feats = _query(page, pat)
            time.sleep(random.uniform(0.3, 0.6))
            m = _match({"address": address}, feats)
            if m:
                return m
    return None


def enrich_db(limit: int = 200) -> Dict[str, int]:
    """Enrich active Knox rows missing KGIS data. Returns counts."""
    from . import db as D
    from .config import config
    from .base import camoufox_context

    conn = D._ensure_db(config.db_path)
    rows = conn.execute(
        "SELECT id, address, city, county, parcel_number, manual_acres_set "
        "FROM properties "
        "WHERE status='active' AND county='Knox' AND state='TN' "
        "AND (gis_url IS NULL OR gis_url NOT LIKE '%kgis.org%') "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    todo = [dict(r) for r in rows if (dict(r).get("address") or "").strip()
            or (dict(r).get("parcel_number") or "").strip()]
    stats = {"processed": 0, "enriched": 0, "unmatched": 0, "failed": 0}
    if not todo:
        conn.close()
        return stats

    from .base import camoufox_context
    with camoufox_context() as page:
        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto("https://www.kgis.org/kgismaps/Map.htm",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        for prop in todo:
            pid = prop["id"]
            stats["processed"] += 1
            try:
                m = lookup(page, prop.get("address") or "",
                           prop.get("parcel_number") or "")
            except Exception as e:
                logger.warning("KGIS lookup failed #%s: %s", pid, e)
                stats["failed"] += 1
                continue
            if not m or not m.get("parcel_id"):
                stats["unmatched"] += 1
                continue
            gis = (f"https://www.kgis.org/kgismaps/Map.htm"
                   f"?parcel={urllib.parse.quote(m['parcel_id'])}")
            manual = (prop.get("manual_acres_set") or "").strip()
            if m.get("acres") and not manual:
                conn.execute(
                    "UPDATE properties SET acres=?, acres_source='kgis', "
                    "owner_name=?, gis_url=? WHERE id=?",
                    (m["acres"], m.get("owner") or None, gis, pid),
                )
            else:
                conn.execute(
                    "UPDATE properties SET owner_name=COALESCE(owner_name, ?), "
                    "gis_url=? WHERE id=?",
                    (m.get("owner") or None, gis, pid),
                )
            sdb_log(conn, pid, m)
            conn.commit()
            stats["enriched"] += 1
            # Below-threshold rows archive like every other GIS fill.
            if m.get("acres") and m["acres"] < config.MIN_ACRES and not manual:
                D.archive_property(conn, pid, reason="below_min_acres (kgis)",
                                   archived_by="system")
    conn.close()
    return stats


def sdb_log(conn, pid: int, m: dict) -> None:
    from . import db as D
    try:
        D.log_audit(conn, actor="system", action="kgis_enrich",
                    property_id=pid,
                    reason=f"parcel={m.get('parcel_id')} acres={m.get('acres')}",
                    prev_status="active")
    except Exception as e:
        logger.warning("audit log failed #%s: %s", pid, e)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=200)
    print(enrich_db(limit=p.parse_args().limit))
