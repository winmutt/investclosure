"""Re-derive the description and address for ky_publicnotice rows.

Kentucky notices are full newspaper-page PDFs whose columns interleave the
sale notice with ads, calendars and unrelated classifieds. Two defects
surfaced on row #904 (Mountain Advocate A6, Knox Co.):

1. the description was ``text[:2000]`` — the page BEGINNING (a recurring
   community calendar), so the row read as unrelated to any sale; it is now
   the window around the strongest sale marker;
2. the address extractor returned the first address-shaped capture inside
   the marker window — "4 p.m. CT. KNOX CIRCUIT COURT" — where the 'CT'
   abbreviation matched the Court street suffix, while the genuinely
   labeled "STREET ADDRESS: 575 Leo Hampton Road" lost in document order.

For every ``ky_publicnotice`` row this re-derives the description (the
sale-marker window) and the address (labeled captures preferred over bare
ones, time/court-heading debris rejected) from ``raw_source_text`` and
updates both when they differ. The stored address is overwritten even when
non-empty: the upsert only backfills address fields when EMPTY, so a junk
capture would otherwise be pinned forever.

    python3 -m scraper.backfill_ky_notices [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from typing import Optional

from .config import config
from .ky_publicnotice import (
    KYPublicNoticeScraper,
    _best_marker_pos,
)
from .publicnotice_base import normalize_notice_text

logger = logging.getLogger(__name__)


def _derive(text: str, county: str) -> tuple[str, Optional[str]]:
    """(windowed description, address) from a stored newspaper page."""
    body = normalize_notice_text(text)
    marker = _best_marker_pos(body.lower())
    if marker is not None:
        desc = re.sub(r"\s+", " ", body[max(0, marker - 300):marker + 2300]).strip()
    else:
        desc = body[:2000]
    addr = KYPublicNoticeScraper._extract_address(text, county)
    return desc, addr


def backfill_ky_notices(apply: bool = True) -> dict:
    conn = sqlite3.connect(str(config.db_path))
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, county, address, description, raw_source_text, "
        "description IS NOT NULL AS has_desc FROM properties "
        "WHERE source='ky_publicnotice' AND raw_source_text != '' "
        "ORDER BY id").fetchall()

    stats = {"rows": 0, "addr_fixed": 0, "desc_fixed": 0, "unchanged": 0}

    for row in rows:
        stats["rows"] += 1
        text = row["raw_source_text"] or ""
        desc, addr = _derive(text, row["county"] or "")
        update: dict = {}
        if desc and desc != (row["description"] or ""):
            update["description"] = desc
        if addr and addr != (row["address"] or ""):
            update["address"] = addr
        if not update:
            stats["unchanged"] += 1
            continue
        if "description" in update:
            stats["desc_fixed"] += 1
        if "address" in update:
            stats["addr_fixed"] += 1
        logger.info("KY notice fix #%s (%s): %s",
                    row["id"], row["county"],
                    {k: (v[:60] + "…" if len(v) > 60 else v)
                     for k, v in update.items()})
        if apply:
            set_parts = ", ".join(f"{k} = ?" for k in update)
            conn.execute(
                f"UPDATE properties SET {set_parts}, "
                "last_updated = CURRENT_TIMESTAMP WHERE id = ?",
                list(update.values()) + [row["id"]])
            conn.commit()

    conn.close()
    logger.info("KY notice backfill complete: %s", stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(backfill_ky_notices(apply=not args.dry_run))
