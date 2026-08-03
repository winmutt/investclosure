"""CLI runner for investclosure — property scrapers.

Run a single scraper, all, or run in continuous/cron mode.

Usage:
    python3 -m scraper --list            # List available scrapers
    python3 -m scraper --scraper kania_law      # Run Kania Law only
    python3 -m scraper --scraper zls_nc         # Run ZLS-NC only
    python3 -m scraper --scraper newspaper_notices  # Run newspaper notices only
    python3 -m scraper --all             # Run all active scrapers
    python3 -m scraper --cron            # Run on schedule (every SCRAPE_INTERVAL minutes)
    python3 -m scraper --status          # Show DB stats
    python3 -m scraper --new             # Show new properties since last run
    python3 -m scraper --archive         # Archive below threshold
    python3 -m scraper --enrich          # Enrich DB properties with NC OneMap GIS data
"""
from __future__ import annotations
import logging
import sys
import sqlite3
import json
import argparse
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# Ensure scraper package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.db import _ensure_db, update_scrape_run, get_stats, get_new_since, archive_below_acres, insert_property, get_all_active
from scraper.config import config

logger = logging.getLogger(__name__)

# Map scraper names to classes
# DISABLED: ncforeclosures, tnforeclosures, tnmap — broken/unreliable
SCRAPER_MODULES: dict = {}

try:
    from scraper.kania_law import KaniaLawScraper
    SCRAPER_MODULES["kania_law"] = KaniaLawScraper
except ImportError as e:
    logger.warning("kania_law not available: %s", e)

try:
    from scraper.zls_nc import ZLSNCScraper
    SCRAPER_MODULES["zls_nc"] = ZLSNCScraper
except ImportError as e:
    logger.warning("zls_nc not available: %s", e)

try:
    from scraper.hutchens_law import HutchensLawScraper
    SCRAPER_MODULES["hutchens_law"] = HutchensLawScraper
except ImportError as e:
    logger.warning("hutchens_law not available: %s", e)

try:
    from scraper.newspaper_notices import NewspaperNoticesScraper
    SCRAPER_MODULES["newspaper_notices"] = NewspaperNoticesScraper
except ImportError as e:
    logger.warning("newspaper_notices not available: %s", e)


# ---------------------------------------------------------------------------
# Core run logic
# ---------------------------------------------------------------------------

def run_scraper(conn: sqlite3.Connection, scraper_name: str, scraper_class) -> dict:
    """Run a single scraper, save results to DB, return stats."""
    logger.info("%s SCRAPER", scraper_name.upper())
    run_id = _start_logging(conn, scraper_name)

    try:
        # Check if scraper is disabled due to failures
        if _is_scraper_disabled(scraper_name):
            logger.warning("%s DISABLED: Failed %d consecutive runs", scraper_name.upper(), _get_failure_count(scraper_name))
            return {"scraper": scraper_name, "found": 0, "new": 0, "error": "SCRAPER_DISABLED"}

        if scraper_name == "tnforeclosures":
            properties = scrape_with_enrichment(solve_captcha=True, enrich=True)
        elif scraper_name == "ncforeclosures":
            scraper = scraper_class()
            properties = scraper.run()
        else:
            scraper = scraper_class()
            properties = scraper.run() if hasattr(scraper, 'run') else []
    except Exception as e:
        logger.error("%s FAILED: %s", scraper_name, e, exc_info=True)
        _inc_failure_counter(scraper_name)
        _end_logging(conn, run_id, 0, 0, 0, "failed", str(e))
        return {"scraper": scraper_name, "found": 0, "new": 0, "error": str(e)}

    new_count = 0
    dup_count = 0

    for prop in properties:
        try:
            price = prop.get("price")
            price_cents = int(price * 100) if price else 0

            action, row = insert_property(
                conn,
                source=scraper_name,
                source_listing_id=prop.get("source_listing_id"),
                url=prop.get("url"),
                address=prop.get("address"),
                city=prop.get("city"),
                county=prop.get("county"),
                state=prop.get("state"),
                zip_code=prop.get("zip_code"),
                latitude=prop.get("latitude"),
                longitude=prop.get("longitude"),
                price_cents=price_cents,
                acres=prop.get("acres"),
                description=prop.get("description"),
                property_type=prop.get("property_type"),
                auction_date=prop.get("auction_date"),
                close_date=prop.get("close_date"),
                upset_bid=prop.get("upset_bid"),
                foreclosure_key=prop.get("foreclosure_key"),
                parcel_number=prop.get("parcel_number"),
                deed_book=prop.get("deed_book"),
                google_maps_url=prop.get("google_maps_url"),
                google_maps_topo_url=prop.get("google_maps_topo_url"),
                gis_url=prop.get("gis_url"),
                elevation_ft=prop.get("elevation_ft"),
                parcel_screenshot=prop.get("parcel_screenshot"),
            )

            if action == "duplicate":
                dup_count += 1
            else:
                new_count += 1
                logger.info(
                    "[NEW] %s | %.1fac | %s, %s",
                    prop.get("county") or "?", prop.get("acres") or 0,
                    prop.get("county") or "?", prop.get("state") or "?",
                )

        except Exception as e:
            logger.error("Failed to save property: %s", e, exc_info=True)

    # Log scrape run completion
    _end_logging(conn, run_id, len(properties), new_count, dup_count, "completed")

    # Track success
    _reset_failure_counter(scraper_name)

    return {
        "scraper": scraper_name,
        "found": len(properties),
        "new": new_count,
        "duplicates": dup_count,
        "disabled": _is_scraper_disabled(scraper_name),
    }


def _get_failure_count(scraper_name: str) -> int:
    """Get consecutive failure count for a scraper."""
    logs_dir = config.logs_dir / "scraper_failures"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fpath = logs_dir / f"{scraper_name}.json"
    if fpath.exists():
        with open(fpath) as f:
            return json.load(f).get("count", 0)
    return 0


def _reset_failure_counter(scraper_name: str) -> None:
    """Reset failure counter for a scraper."""
    logs_dir = config.logs_dir / "scraper_failures"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fpath = logs_dir / f"{scraper_name}.json"
    if fpath.exists():
        fpath.unlink()


def _is_scraper_disabled(scraper_name: str, max_failures: int = 3) -> bool:
    """Check if scraper is disabled due to too many failures."""
    count = _get_failure_count(scraper_name)
    return count >= max_failures

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
    for name in sorted(SCRAPER_MODULES):
        print(f"  - {name}")
    print(f"\nTotal: {len(SCRAPER_MODULES)} scrapers")


def cmd_run(scraper_name: str) -> dict:
    """Run a single scraper."""
    scraper_class = SCRAPER_MODULES.get(scraper_name)
    if not scraper_class:
        print(f"Unknown scraper: {scraper_name}")
        print(f"Available: {', '.join(sorted(SCRAPER_MODULES))}")
        return {}

    conn = _ensure_db()
    try:
        result = run_scraper(conn, scraper_name, scraper_class)
        print(f"\n  {scraper_name}: found={result['found']}, new={result['new']}, dups={result.get('duplicates', 0)}")
        return result
    finally:
        conn.close()


def cmd_run_all() -> list[dict]:
    """Run all scrapers and auto-archive small-acreage properties."""
    results = []
    total_found = 0
    total_new = 0

    for name, cls in SCRAPER_MODULES.items():
        print(f"\n{'='*60}")
        result = cmd_run(name)
        if result:
            results.append(result)
            total_found += result.get("found", 0)
            total_new += result.get("new", 0)

    # Auto-archive properties with 0 < acres < 2
    print(f"\n{'='*60}")
    print(f"  Auto-archiving properties with 0 < acres < 2 ...")
    conn = _ensure_db()
    try:
        archived = archive_below_acres(conn, min_acres=2.0, include_sources=list(SCRAPER_MODULES.keys()))
        conn.close()
        if archived:
            print(f"  Auto-archived: {archived} properties")
        else:
            print(f"  No properties to auto-archive")
    except Exception as e:
        conn.close()
        logger.warning("Auto-archive failed: %s", e)

    print(f"\n  TOTAL: found={total_found}, new={total_new}")
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
        for source in SCRAPER_MODULES:
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
                    ac = p.get('acres')
                    ac_str = f"{ac:.1f}" if ac is not None else "?"
                    print(f"    {p['county']}, {p['state']}  {ac_str}ac  {p.get('description', '')[:80]}")
            else:
                print(f"\n  [{source}] No new properties since {last_at}")
    finally:
        conn.close()


def cmd_enrich(source: Optional[str] = None) -> dict:
    """Enrich properties in DB with NC OneMap GIS parcel data."""
    from scraper.nc_gis_lookup import enrich_properties
    
    result = enrich_properties(source=source)
    return result


def cmd_archive(min_acres: float | None = None) -> int:
    """Archive properties below MIN_ACRES threshold."""
    conn = _ensure_db()
    try:
        threshold = min_acres or config.MIN_ACRES
        archived = archive_below_acres(conn, threshold, include_sources=list(SCRAPER_MODULES.keys()))
        print(f"\n  Archived: {archived} properties (threshold={threshold}ac)")
        return archived
    finally:
        conn.close()


def cmd_cron(minutes: int = 360) -> None:
    """Run in continuous mode — execute scrapers every `minutes` minutes."""
    print(f"\n  CRON MODE: running every {minutes} minutes (Ctrl+C to stop)")
    print(f"  Scrapers: {', '.join(sorted(SCRAPER_MODULES))}")
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
        choices=list(SCRAPER_MODULES.keys()),
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
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich properties in DB with NC OneMap GIS parcel data",
    )
    parser.add_argument(
        "--enrich-source",
        help="Enrich only properties from a specific source (e.g., 'kania_law')",
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
    elif args.enrich:
        result = cmd_enrich(args.enrich_source)
        if isinstance(result, dict):
            print(f"\n  Enrichment complete: {result.get('enriched', 0)} enriched, "
                  f"{result.get('skipped_already_gis', 0)} skipped(gis), "
                  f"{result.get('skipped_no_parcel', 0)} skipped(no parcel), "
                  f"{result.get('failed', 0)} failed")
        else:
            print(f"\n  Enrichment complete: {result} updated")
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
