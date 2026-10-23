"""Re-derive auction_date for public-notice rows from ``raw_source_text``.

The mortgage-foreclosure date extractors had a blind spot: deed-recital
anchors ("dated"/"recorded"/"executed") were only searched BEFORE a
candidate date, but HUD-style recitals read
"on December 7, 2009, a certain Deed of Trust was executed" — the anchor
TRAILS the date, so the loan/deed date was stored as the auction date
(rows #899/#785 NC, #885 AL). NC also missed the labeled "DATE OF SALE:"
field (#897) and weekday-prefixed "on Thursday, <date>" candidates, and
AL's OCR hyphen-splits months ("Novem- ber 19") so the true date often
failed to parse at all.

For every active ``nc_publicnotice`` / ``al_publicnotice`` /
``ky_publicnotice`` row (tax + mortgage notices share these extractors)
the per-state extractor re-derives the auction date; a row is updated only
when the new value is non-empty AND denotes a different calendar day from
the stored one (``None`` stored stays stored — no date is better than a
wrong one, but we also do not erase a possibly-correct value the new logic
cannot see).

    python3 -m scraper.backfill_publicnotice_dates [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from datetime import datetime
from typing import Optional

from .config import config

logger = logging.getLogger(__name__)

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_SOURCES = ("nc_publicnotice", "al_publicnotice", "ky_publicnotice")


def _new_extractor():
    from .nc_publicnotice import _find_auction_date
    from .al_publicnotice import ALPublicNoticeScraper
    from .ky_publicnotice import KYPublicNoticeScraper
    return {
        "nc_publicnotice": _find_auction_date,
        "al_publicnotice": ALPublicNoticeScraper._extract_sale_date,
        "ky_publicnotice": KYPublicNoticeScraper._extract_sale_date,
    }


def _stored_to_iso(stored: Optional[str]) -> Optional[str]:
    """Stored auction_date -> ISO, when it is a plain display date."""
    s = (stored or "").strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def backfill_publicnotice_dates(apply: bool = True) -> dict:
    extractors = _new_extractor()
    conn = sqlite3.connect(str(config.db_path))
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, source, county, property_type, status, auction_date, "
        "raw_source_text, description FROM properties "
        f"WHERE source IN ({', '.join('?' * len(_SOURCES))}) "
        "ORDER BY source, id", _SOURCES).fetchall()

    stats = {"rows": 0, "updated": 0, "unchanged": 0, "no_new_date": 0}

    for row in rows:
        stats["rows"] += 1
        text = row["raw_source_text"] or row["description"] or ""
        got = extractors[row["source"]](text)

        if not got:
            stats["no_new_date"] += 1
            continue

        stored = row["auction_date"]
        if _same_date(stored, got):
            stats["unchanged"] += 1
            continue

        stats["updated"] += 1
        logger.info("Auction-date fix #%s %s %s (%s): %r -> %r",
                    row["id"], row["source"], row["county"],
                    row["property_type"], stored, got)
        if apply:
            # Written in the extractor's own display format so the value
            # stays consistent with what the live scrapers insert for the
            # source (NC month-name dates, AL/KY ISO dates).
            conn.execute(
                "UPDATE properties SET auction_date = ?, "
                "last_updated = CURRENT_TIMESTAMP WHERE id = ?",
                (got, row["id"]))
            conn.commit()
    conn.close()
    logger.info("PublicNotice date backfill complete: %s", stats)
    return stats


def _same_date(a: Optional[str], got: str) -> bool:
    """True when the stored and freshly-derived dates denote the same day,
    regardless of display format (month-name vs ISO)."""
    got_iso = _stored_to_iso(got) or got
    return _stored_to_iso(a) == got_iso


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(backfill_publicnotice_dates(apply=not args.dry_run))
