"""JSONL raw-notice logger for scraper quality evaluation.

Every public/legal-notice scraper logs one JSON object per candidate notice
— kept or dropped — including the full raw source text, so a run can be
audited for classifier quality (tax vs mortgage vs drop) without re-scraping.
Files live in ``scraper/tmp/`` (volume-mounted into the container) as
``<source>_raw_<YYYYMMDD>.jsonl``.

Decision vocabulary (kept in sync across scrapers):
    kept_tax, kept_mortgage,
    dropped_publication, dropped_non_foreclosure, dropped_quiet_title,
    dropped_post_sale, dropped_short, dropped_county, dropped_acres,
    dropped_no_key
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def rawlog_path(source: str, directory: Optional[Path] = None) -> Path:
    """Path of today's JSONL log for *source* (host + container safe)."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    base = directory or (Path(__file__).resolve().parent / "tmp")
    return base / f"{source}_raw_{day}.jsonl"


def log_raw(
    source: str,
    *,
    listing_id=None,
    county=None,
    state=None,
    decision: str = "",
    reason: str = "",
    raw_text: str = "",
    url=None,
    directory: Optional[Path] = None,
) -> None:
    """Append one JSON line for a candidate notice. Never raises."""
    try:
        text = raw_text or ""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "listing_id": listing_id,
            "county": county,
            "state": state,
            "decision": decision,
            "reason": reason,
            "chars": len(text),
            "raw_source_text": text,
            "url": url,
        }
        path = rawlog_path(source, directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:  # logging must never break a scrape
        logger.warning("rawlog write failed for %s %s: %s", source, listing_id, e)
