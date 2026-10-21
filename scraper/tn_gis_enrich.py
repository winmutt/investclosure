"""Enrich TN properties via TNMap assessment search (owner, acres, TPAD link).

TNMap geocodes by street address, so only rows with a house number are
eligible — road-name-only rows can never match and are skipped. Runs one
shared pass over active TN rows lacking a TPAD deep link; safe to re-run
(it self-filters to unenriched rows).
"""
from __future__ import annotations
import logging
import re
from typing import Dict, List

logger = logging.getLogger(__name__)

_HOUSE_NUMBER_RE = re.compile(r"^\s*\d+\s+")


def _has_house_number(address: object) -> bool:
    return bool(_HOUSE_NUMBER_RE.search(str(address or "")))


def enrich_db(limit: int = 200) -> Dict[str, int]:
    """Enrich active TN rows missing a TPAD link (or with NULL acres after one).

    Self-filter: rows that already have a TPAD link but no acres are
    re-processed too — the cadastral layer frequently stores DEEDAC=0/CALCAC=0
    for them, and the polygon-area fallback only fills on a later pass.
    """
    from . import db as D
    from .config import config
    from .tnmap import enrich_with_tnmap

    conn = D._ensure_db(config.db_path)
    rows = conn.execute(
        "SELECT id, address, city, county, state FROM properties "
        "WHERE status='active' AND state='TN' "
        "AND ((gis_url IS NULL OR gis_url NOT LIKE '%TPAD%') "
        "     OR (tnmap_data IS NOT NULL AND acres IS NULL)) "
        "LIMIT ?",
        (limit,),
    ).fetchall()
    todo = [dict(r) for r in rows
            if _has_house_number(r["address"]) and (r["county"] or "").strip()]
    stats = {"processed": 0, "enriched": 0, "unmatched": 0, "failed": 0}
    if not todo:
        conn.close()
        return stats

    try:
        enriched = enrich_with_tnmap(todo)
    except Exception as e:
        logger.warning("TNMap batch failed: %s", e)
        conn.close()
        stats["failed"] = len(todo)
        return stats

    for prop in enriched:
        stats["processed"] += 1
        pid = prop.get("id")
        if not pid:
            stats["failed"] += 1
            continue
        if not prop.get("tnmap_gislink"):
            stats["unmatched"] += 1
            continue
        try:
            D.update_tnmap_enrichment(
                conn, pid,
                owner_name=prop.get("owner_name"),
                acres=prop.get("acres"),
                acres_source=prop.get("acres_source"),
                gis_url=prop.get("gis_url"),
                google_maps_url=prop.get("google_maps_url"),
                google_maps_topo_url=prop.get("google_maps_topo_url"),
                tnmap_data=prop.get("tnmap_data"),
            )
            stats["enriched"] += 1
        except Exception as e:
            logger.warning("TNMap persist failed for #%s: %s", pid, e)
            stats["failed"] += 1
    conn.commit()
    conn.close()
    return stats


if __name__ == "__main__":
    print(enrich_db())
