"""CLI runner for investclosure foreclosures.

Run a single scraper, both, or run in continuous/cron mode.

Usage:
    python -m scraper.run --list            # List available scrapers
    python -m scraper.run --scraper ncforeclosures   # Run NC only
    python -m scraper.run --scraper tnforeclosures   # Run TN only
    python -m scraper.run --all             # Run both
    python -m scraper.run --cron            # Run on schedule (every SCRAPE_INTERVAL minutes, default 360)
    python -m scraper.run --status          # Show DB stats
    python -m scraper.run --new             # Show new properties since last run
    python -m scraper.run --archive         # Archive properties below MIN_ACRES
"""
from __future__ import annotations
import logging
import sys
import sqlite3
import argparse
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# Ensure scraper package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.db import _ensure_db, update_scrape_run, get_stats, get_new_since, archive_below_acres
from scraper.config import config
from scraper.ncforeclosures import NCForeclosureScraper
from scraper.tnforeclosures import TNForeclosureScraper

logger = logging.getLogger(__name__)

# Map scraper names to classes
SCRAPERS = {
    "ncforeclosures": NCForeclosureScraper,
    "tnforeclosures": TNForeclosureScraper,
}


# ---------------------------------------------------------------------------
# Core run logic
# ---------------------------------------------------------------------------

def run_scraper(conn: sqlite3.Connection, scraper_name: str, scraper_class) -> dict:
    """Run a single scraper, save results to DB, return stats."""
    logger.info("%s SCRAPER", scraper_name.upper())
    run_id = _start_logging(conn, scraper_name)

    try:
        scraper = scraper_class()
        properties = scraper.run()
    except Exception as e:
        logger.error("%s FAILED: %s", scraper_name, e, exc_info=True)
        _end_logging(conn, run_id, 0, 0, 0, "failed", str(e))
        return {"scraper": scraper_name, "found": 0, "new": 0, "error": str(e)}

    new_count = 0
    dup_count = 0

    for prop in properties:
        try:
            acres = float(prop.get("acres", 0)) if prop.get("acres") else 0.0

            action, row = conn.execute(
                "SELECT id FROM properties WHERE source=? AND source_listing_id=? LIMIT 1",
                (scraper_name, prop.get("source_listing_id")),
            ).fetchone()

            if action is not None:
                dup_count += 1
            else:
                new_count += 1
                logger.info(
                    "[NEW] %s | %.1fac | %s, %s",
                    prop.get("county", "?"), acres,
                    prop.get("county", "?"), prop.get("state", "?"),
                )

        except Exception as e:
            logger.error("Failed to save property: %s", e, exc_info=True)

    _end_logging(conn, run_id, len(properties), new_count, dup_count, "completed")

    return {
        "scraper": scraper_name,
        "found": len(properties),
        "new": new_count,
        "duplicates": dup_count,
    }


def _start_logging(
    conn: sqlite3.Connection,
    source: str,
) -> int:
    """Start a scrape run. Returns run id."""
    cur = conn.execute(
        "INSERT INTO scrape_runs (source) VALUES (?)", (source,)
    )
    conn.commit()
    return cur.lastrowid


def _end_logging(
    conn: sqlite3.Connection,
    run_id: int,
    found: int,
    new_count: int,
    dup_count: int,
    status: str,
    error_message: str | None = None,
) -> None:
    """End a scrape run."""
    conn.execute(
        """UPDATE scrape_runs
           SET finished_at = datetime('now'),
               properties_found = ?,
               properties_new = ?,
               properties_duplicate = ?,
               status = ?,
               error_message = ?
           WHERE id = ?""",
        (found, new_count, dup_count, status, error_message, run_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_list() -> None:
    """List available scrapers."""
    print("Available scrapers:")
    for name in sorted(SCRAPERS):
        print(f"  - {name}")
    print(f"\nTotal: {len(SCRAPERS)} scrapers")


def cmd_run(scraper_name: str) -> dict:
    """Run a single scraper."""
    scraper_class = SCRAPERS.get(scraper_name)
    if not scraper_class:
        print(f"Unknown scraper: {scraper_name}")
        print(f"Available: {', '.join(sorted(SCRAPERS))}")
        return {}

    conn = _ensure_db()
    try:
        result = run_scraper(conn, scraper_name, scraper_class)
        print(f"\n  {scraper_name}: found={result['found']}, new={result['new']}, dups={result.get('duplicates', 0)}")
        return result
    finally:
        conn.close()


def cmd_run_all() -> list[dict]:
    """Run all scrapers."""
    results = []
    total_found = 0
    total_new = 0

    for name, cls in SCRAPERS.items():
        print(f"\n{'='*60}")
        result = cmd_run(name)
        if result:
            results.append(result)
            total_found += result.get("found", 0)
            total_new += result.get("new", 0)

    print(f"\n{'='*60}")
    print(f"  TOTAL: found={total_found}, new={total_new}")
    print(f"{'='*60}\n")
    return results


def cmd_status() -> None:
    """Show DB stats."""
    conn = _ensure_db()
    try:
        stats = get_stats(conn)
        print("\n  DB Stats:")
        print(f"    Active properties:    {stats.get('total_active', 0)}")
        print(f"    Total seen:           {stats.get('total_seen', 0)}")
        print(f"    New today:            {stats.get('today_new', 0)}")
        print(f"    Archived:             {stats.get('total_archived', 0)}")
        print(f"    Scrape runs:          {stats.get('scrape_runs', 0)}")

        by_source = stats.get("by_source", [])
        if by_source:
            print(f"\n    By source:")
            for name, cnt in by_source:
                print(f"      {name:<20} {cnt}")

        by_county = stats.get("by_county", [])
        if by_county:
            print(f"\n    Top counties:")
            for name, cnt in by_county[:10]:
                print(f"      {name:<35} {cnt}")
        print()
    finally:
        conn.close()


def cmd_new() -> None:
    """Show new properties since last run."""
    conn = _ensure_db()
    try:
        # Get the last completed run per source
        last_runs = {}
        for source in SCRAPERS:
            row = conn.execute(
                "SELECT finished_at FROM scrape_runs WHERE source=? AND status='completed' ORDER BY finished_at DESC LIMIT 1",
                (source,),
            ).fetchone()
            if row:
                last_runs[source] = row["finished_at"]

        for source, last_at in last_runs.items():
            props = get_new_since(conn, since_date=last_at, source=source)
            if props:
                print(f"\n  [{source}] {len(props)} new since {last_at}:")
                for p in props:
                    print(f"    {p['county']}, {p['state']}  {p['acres']:.1f}ac  {p.get('description', '')[:80]}")
            else:
                print(f"\n  [{source}] No new properties since {last_at}")
    finally:
        conn.close()


def cmd_archive(min_acres: float | None = None) -> int:
    """Archive properties below MIN_ACRES threshold."""
    conn = _ensure_db()
    try:
        threshold = min_acres or config.MIN_ACRES
        archived_nc = archive_below_acres(conn, threshold, "ncforeclosures")
        archived_tn = archive_below_acres(conn, threshold, "tnforeclosures")
        total = archived_nc + archived_tn
        print(f"\n  Archived: NC={archived_nc}, TN={archived_tn}, Total={total} (threshold={threshold}ac)")
        return total
    finally:
        conn.close()


def cmd_cron(minutes: int = 360) -> None:
    """Run in continuous mode — execute scrapers every `minutes` minutes."""
    print(f"\n  CRON MODE: running every {minutes} minutes (Ctrl+C to stop)")
    print(f"  Scrapers: {', '.join(sorted(SCRAPERS))}")
    print()

    while True:
        try:
            cmd_run_all()
            print(f"\n  Next run in {minutes} minutes...")
            time.sleep(minutes * 60)
        except KeyboardInterrupt:
            print("\n  Stopped.")
            break
        except Exception as e:
            logger.error("Cron loop error: %s", e, exc_info=True)
            time.sleep(60)  # Wait 1 min before retry


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="investclosure",
        description="Foreclosure property scraper for NC and TN",
    )
    parser.add_argument(
        "--scraper", "-s",
        choices=list(SCRAPERS.keys()),
        help="Run a specific scraper",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Run all scrapers",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available scrapers",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show DB stats",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="Show new properties since last run",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Archive properties below MIN_ACRES threshold",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        help="Override MIN_ACRES for archive",
    )
    parser.add_argument(
        "--cron",
        action="store_true",
        help="Run continuously, every SCRAPE_INTERVAL minutes",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=360,
        help="Cron interval in minutes (default 360)",
    )

    args = parser.parse_args()

    # Set up logging
    log_file = config.logs_dir / "investclosure.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_file)),
            logging.StreamHandler(),
        ],
    )

    if args.list:
        cmd_list()
    elif args.status:
        cmd_status()
    elif args.new:
        cmd_new()
    elif args.archive:
        cmd_archive(args.threshold)
    elif args.cron:
        cmd_cron(args.interval)
    elif args.scraper:
        cmd_run(args.scraper)
    elif args.all or not any([args.list, args.status, args.new, args.archive, args.cron, args.scraper]):
        cmd_run_all()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
