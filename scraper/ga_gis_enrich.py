"""Enrich GA properties with acreage from county qPublic parcel reports.

Georgia has no statewide parcel hub, but each mountain county's qPublic
(Schneider Corp) app serves a parcel report page that lists acreage. Plain
HTTP gets Cloudflare-403, but the camoufox stealth browser loads these
reports fine — so enrichment runs through camoufox page loads (one shared
browser session per batch, ~15s per parcel, 2s+ between loads).

Only fills rows that lack acreage; a report of "Acres 0" (e.g. Rabun Sky
Valley subdivision lots) leaves the row NULL — correctly bypassing the
<MIN_ACRES archive filter and showing "N/A ac".
"""
from __future__ import annotations
import logging
import re
import time
from typing import Dict, List, Optional

from .base import camoufox_context
from .gis_urls import GA_QPUBLIC_APPS, get_ga_gis_url

logger = logging.getLogger(__name__)

QPUBLIC_ACRES_RE = re.compile(r"Acres\s+([0-9][0-9,]*\.?\d*)", re.IGNORECASE)


def parse_qpublic_acres(body: str) -> Optional[float]:
    """Extract the Acres value from a qPublic report body, or None."""
    if not body:
        return None
    m = QPUBLIC_ACRES_RE.search(body)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if not (0 <= value < 100000):
        return None
    return value


def fetch_qpublic_acres(page, county: str, parcel: str) -> Optional[float]:
    """Load one county report page in an existing camoufox page; parse acres."""
    url = get_ga_gis_url(county, parcel)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)
        body = page.inner_text("body") or ""
    except Exception as e:
        logger.warning("qPublic load failed for %s %s: %s", county, parcel, e)
        return None
    return parse_qpublic_acres(body)


def enrich_db(limit: int = 100) -> Dict[str, int]:
    """Fill NULL acreage on GA rows from qPublic reports. Returns counts."""
    from . import db as D
    from .config import config

    conn = D._ensure_db(config.db_path)
    rows = conn.execute(
        "SELECT id, county, parcel_number FROM properties "
        "WHERE state='GA' AND acres IS NULL "
        "AND parcel_number IS NOT NULL AND TRIM(parcel_number) != '' "
        "LIMIT ?",
        (limit,),
    ).fetchall()

    stats = {"processed": 0, "updated": 0, "zero_or_missing": 0,
             "skipped_no_app": 0, "failed": 0}
    todo = [(r["id"], (r["county"] or "").strip().lower(),
             (r["parcel_number"] or "").strip()) for r in rows]
    todo = [t for t in todo if t[2]]
    if not todo:
        conn.close()
        return stats

    with camoufox_context() as page:
        for pid, county, parcel in todo:
            stats["processed"] += 1
            if county not in GA_QPUBLIC_APPS:
                stats["skipped_no_app"] += 1
                continue
            try:
                acres = fetch_qpublic_acres(page, county, parcel)
            except Exception as e:
                logger.warning("qPublic enrich failed for %s %s: %s",
                               county, parcel, e)
                acres = None
                stats["failed"] += 1
            time.sleep(2)
            if acres is not None and acres > 0:
                conn.execute("UPDATE properties SET acres=? WHERE id=?",
                             (acres, pid))
                stats["updated"] += 1
                logger.info("qPublic acres %s %s -> %s", county, parcel, acres)
            else:
                stats["zero_or_missing"] += 1
    conn.commit()
    conn.close()
    return stats


if __name__ == "__main__":
    print(enrich_db())
