"""Backfill classification/address/date fixes into newspaper_notices rows.

The BLOX/AdPerfect newspaper sub-scrapers (Sylva Herald, Watauga Democrat,
Avery Journal, Transylvania Times, Mitchell News) previously hardcoded
``address=None`` (the notice body publishes the street address but the
parsers dropped it) and stored the *publication* date as the auction date.
The HOA assessment-lien foreclosure #827 also landed on the Tax tab because
the surviving-lien boilerplate's "unpaid taxes" aside was counted as a
tax-sale signal.

For every newspaper_notices row this re-derives, from ``raw_source_text``:
  1. property_type via :func:`_classify_newspaper_notice` (HOA assessment
     liens move off the Tax tab to the Mtg tab; nothing is un-archived).
  2. address / city / zip (new "Property address:" etc. patterns).
  3. auction_date via ``_extract_auction_date`` when the notice states the
    actual sale date ("on Oct 29, 2026 at 2:30 PM").

Then run ``python3 -m scraper.nc_gis_lookup``-style enrichment:
    python3 -m scraper.backfill_newspaper [--dry-run]
    python3 -m scraper --enrich --enrich-source newspaper_notices   # GIS pass
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from typing import Optional

from .config import config
from .newspaper_notices import (
    _classify_newspaper_notice,
    _extract_auction_date,
    extract_newspaper_address,
)

logger = logging.getLogger(__name__)


def backfill_newspaper_rows(apply: bool = True) -> dict:
    conn = sqlite3.connect(str(config.db_path))
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, status, property_type, address, city, zip_code, county, "
        "auction_date, raw_source_text FROM properties "
        "WHERE source = 'newspaper_notices' ORDER BY id"
    ).fetchall()

    stats = {"rows": 0, "reclassified": 0, "address_filled": 0,
             "auction_fixed": 0, "unchanged": 0}

    for row in rows:
        text = row["raw_source_text"] or ""
        county_hint = ""
        update: dict = {}

        kind = _classify_newspaper_notice(text)
        if kind and (kind == "mortgage_foreclosure") != \
                (row["property_type"] == "mortgage_foreclosure"):
            update["property_type"] = kind

        addr, city, zip_code = extract_newspaper_address(text, row["county"] or "")
        if addr and not (row["address"] or "").strip():
            update["address"] = addr
            if city and not (row["city"] or "").strip():
                update["city"] = city
            elif city and zip_code and city.lower() != (row["city"] or "").lower():
                # ZIP-backed city from the notice beats the newspaper's
                # own town (a Sylva paper listing a Sapphire property was
                # storing city='Sylva').
                update["city"] = city
            if zip_code and not (row["zip_code"] or "").strip():
                update["zip_code"] = zip_code

        auction = _extract_auction_date(text)
        stored = (row["auction_date"] or "").strip()
        if auction and auction != stored and not _same_date(row["auction_date"], auction):
            update["auction_date"] = auction

        if not update:
            stats["unchanged"] += 1
            continue

        stats["rows"] += 1
        if "property_type" in update:
            stats["reclassified"] += 1
        if "address" in update:
            stats["address_filled"] += 1
        if "auction_date" in update:
            stats["auction_fixed"] += 1

        logger.info("Newspaper backfill #%s %s: %s", row["id"],
                    row["status"], update)
        if apply:
            set_parts = ", ".join(f"{k} = ?" for k in update)
            conn.execute(
                f"UPDATE properties SET {set_parts}, "
                "last_updated = CURRENT_TIMESTAMP WHERE id = ?",
                list(update.values()) + [row["id"]],
            )
            conn.commit()

    conn.close()
    logger.info("Newspaper backfill complete: %s", stats)
    return stats


def _same_date(a: Optional[str], iso: str) -> bool:
    """True when the stored display date and ISO date denote the same day."""
    from datetime import datetime
    if not a:
        return False
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            if datetime.strptime(a.strip(), fmt).date().isoformat() == iso:
                return True
        except ValueError:
            continue
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(backfill_newspaper_rows(apply=not args.dry_run))
