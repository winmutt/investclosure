"""Sweep Zillow detail pages for HOA fees and archive fee-paying rows.

The county search filter (:func:`scraper.zillow.build_paged_county_url`)
stopped *new* HOA listings from being harvested on 2026-11-07, but the rows
already in the database predate it and the search payload carries no HOA data
at all — only the detail page does. This pass opens every active Zillow
listing once, reads the fee (``monthlyHoaFee``, ``resoFacts.hoaFeeTotal``,
else an explicit fee sentence in the description) and checkpoints the answer
to ``scraper/tmp/zillow_hoa_sweep.jsonl`` so an interrupted run resumes.

Each detail page needs a fresh Camoufox profile (PerimeterX walls a session's
second same-origin navigation), so a full sweep of ~3.2k rows takes hours.
While a scheduled zillow sweep holds a ``scrape_runs`` row open the pass
PAUSES, so the two never hit Zillow concurrently from one IP.

Archiving is a separate mode that replays the checkpoint, so the fee verdicts
can be reviewed before any production row changes:

    python3 -m scraper.zillow_hoa_sweep --limit 20     # smoke test
    python3 -m scraper.zillow_hoa_sweep                # full sweep (hours)
    python3 -m scraper.zillow_hoa_sweep --archive      # apply the verdicts
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import time
from typing import Optional

from .base import camoufox_context
from .db import _ensure_db, archive_property
from .nc_building_harvest import wait_for_sweep
from .zillow import extract_hoa

logger = logging.getLogger(__name__)

PAGE_TIMEOUT_MS = 60000
DELAY_BETWEEN_DETAILS = (1.5, 3.0)
SWEEP_JSONL = os.path.join(os.path.dirname(__file__), "tmp",
                           "zillow_hoa_sweep.jsonl")


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

def load_done() -> dict:
    """Map of already-swept row id -> last JSONL record."""
    done = {}
    if os.path.exists(SWEEP_JSONL):
        with open(SWEEP_JSONL, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    done[int(rec["id"])] = rec
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    return done


def append_record(rec: dict) -> None:
    with open(SWEEP_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_hoa(url: str) -> Optional[dict]:
    """One fresh-profile detail load -> :func:`extract_hoa` result or None."""
    for attempt in (1, 2):
        try:
            with camoufox_context() as page:
                page.set_default_timeout(PAGE_TIMEOUT_MS)
                resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=PAGE_TIMEOUT_MS)
                html = resp.text() if resp else ""
                if len(html or "") < 100_000:
                    logger.warning("detail page too small (%d) for %s%s",
                                   len(html or ""), url,
                                   " — retrying" if attempt == 1 else "")
                    continue
                result = extract_hoa(html)
                if result is None:
                    logger.warning("no property payload for %s%s", url,
                                   " — retrying" if attempt == 1 else "")
                    continue
                return result
        except Exception as exc:
            logger.warning("detail fetch failed for %s (attempt %d): %s",
                           url, attempt, exc)
            time.sleep(5)
    return None


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------

def target_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, url, source_listing_id, address, city, county, state "
        "FROM properties "
        "WHERE status='active' AND source='zillow' AND url IS NOT NULL "
        "ORDER BY id").fetchall()


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def run_pass(limit: Optional[int] = None,
             ids: Optional[set[int]] = None) -> dict:
    conn = _ensure_db()
    # A failed fetch is retried on the next pass: only "ok" verdicts count as
    # done, otherwise a bot wall would permanently leave a row unjudged.
    done = {row_id: rec for row_id, rec in load_done().items()
            if rec.get("status") == "ok"}
    rows = [r for r in target_rows(conn) if r["id"] not in done]
    if ids:
        rows = [r for r in rows if r["id"] in ids]
    if limit:
        rows = rows[:limit]
    stats = {"target": len(rows), "fetched": 0, "failed": 0, "fee": 0}
    logger.info("HOA sweep: %d row(s) to fetch (%d already done)",
                len(rows), len(done))
    for i, row in enumerate(rows, 1):
        wait_for_sweep(conn)
        result = fetch_hoa(row["url"])
        if result is None:
            append_record({
                "id": row["id"], "zpid": row["source_listing_id"],
                "url": row["url"], "county": row["county"],
                "state": row["state"], "address": row["address"],
                "status": "failed", "has_fee": None})
            stats["failed"] += 1
        else:
            append_record({
                "id": row["id"], "zpid": row["source_listing_id"],
                "url": row["url"], "county": row["county"],
                "state": row["state"], "address": row["address"],
                "status": "ok", **result})
            stats["fetched"] += 1
            if result.get("has_fee"):
                stats["fee"] += 1
        if i % 20 == 0 or i == len(rows):
            logger.info("HOA sweep %d/%d — ok=%d fee=%d fail=%d", i, len(rows),
                        stats["fetched"], stats["fee"], stats["failed"])
        time.sleep(random.uniform(*DELAY_BETWEEN_DETAILS))
    logger.info("HOA sweep DONE: %s", stats)
    return stats


# ---------------------------------------------------------------------------
# Archive replay
# ---------------------------------------------------------------------------

def archive_fee_rows(dry_run: bool = False) -> dict:
    """Archive active rows whose checkpointed verdict carries a fee."""
    conn = _ensure_db()
    records = load_done()
    stats = {"checked": 0, "fee": 0, "archived": 0, "already": 0,
             "missing": 0, "dry_run": dry_run}
    conn.row_factory = sqlite3.Row
    for row_id, rec in sorted(records.items()):
        if rec.get("status") != "ok" or not rec.get("has_fee"):
            continue
        stats["checked"] += 1
        stats["fee"] += 1
        row = conn.execute("SELECT id, status, address, county, state "
                           "FROM properties WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            stats["missing"] += 1
            continue
        if row["status"] != "active":
            stats["already"] += 1
            continue
        reason = (f"hoa_fee: {format_fee(rec)} "
                  f"(zillow detail, {rec.get('source') or 'unknown'})")
        logger.info("%s%s %s (%s %s) — %s",
                    "[dry-run] " if dry_run else "", row_id,
                    row["address"], row["county"], row["state"], reason)
        if dry_run:
            continue
        if archive_property(conn, row_id, reason, archived_by="system"):
            stats["archived"] += 1
    logger.info("HOA archive: %s", stats)
    return stats


def format_fee(rec: dict) -> str:
    monthly = rec.get("monthly")
    if not monthly:
        return "fee"
    # Prefer the figure exactly as Zillow publishes it when the feed gave a
    # total ("$450 annually"); monthlyHoaFee is Zillow's normalisation and
    # can disagree with the feed string.
    raw = rec.get("fee_total_raw")
    if raw and isinstance(raw, str):
        return raw
    if rec.get("period") == "annually" and rec.get("amount"):
        return f"${rec['amount']:.0f}/yr (${monthly:.0f}/mo)"
    return f"${monthly:.0f}/mo"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sweep Zillow details for HOA fees")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap rows this invocation (smoke tests)")
    parser.add_argument("--ids", default="",
                        help="comma-separated row ids to sweep")
    parser.add_argument("--archive", action="store_true",
                        help="archive checkpointed fee rows instead of fetching")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --archive: print verdicts, change nothing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if args.archive:
        archive_fee_rows(dry_run=args.dry_run)
        return
    ids = {int(x) for x in args.ids.split(",") if x.strip()}
    run_pass(limit=args.limit, ids=ids or None)


if __name__ == "__main__":
    main()
