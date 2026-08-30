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
import os
import sys
import sqlite3
import json
import argparse
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# Ensure scraper package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.db import _ensure_db, update_scrape_run, get_stats, get_new_since, archive_below_acres, insert_property, get_all_active, update_tnmap_enrichment
from scraper.config import config

logger = logging.getLogger(__name__)

# Map scraper names to classes
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
    from scraper.newspaper_notices import NewspaperNoticesScraper
    SCRAPER_MODULES["newspaper_notices"] = NewspaperNoticesScraper
except ImportError as e:
    logger.warning("newspaper_notices not available: %s", e)

try:
    from scraper.buncombe_tax import BuncombeTaxScraper
    SCRAPER_MODULES["buncombe_tax"] = BuncombeTaxScraper
except ImportError as e:
    logger.warning("buncombe_tax not available: %s", e)

try:
    from scraper.nc_publicnotice import NCPublicNoticeScraper
    SCRAPER_MODULES["nc_publicnotice"] = NCPublicNoticeScraper
except ImportError as e:
    logger.warning("nc_publicnotice not available: %s", e)

try:
    from scraper.tn_publicnotice import scrape_with_enrichment
    SCRAPER_MODULES["tn_publicnotice"] = "tn_publicnotice"
except ImportError as e:
    logger.warning("tn_publicnotice not available: %s", e)

try:
    from scraper.ga_publicnotice import GAPublicNoticeScraper
    SCRAPER_MODULES["ga_publicnotice"] = GAPublicNoticeScraper
except ImportError as e:
    logger.warning("ga_publicnotice not available: %s", e)


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

        if scraper_name == "tn_publicnotice":
            properties = scrape_with_enrichment(solve_captcha=True, enrich=True)
        elif scraper_name == "ga_publicnotice":
            scraper = scraper_class()
            properties = scraper.run()
        elif scraper_name == "nc_publicnotice":
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
                court_case=prop.get("court_case"),
                initial_auction_date=prop.get("initial_auction_date"),
                upset_bid_end=prop.get("upset_bid_end"),
                google_maps_url=prop.get("google_maps_url"),
                google_maps_topo_url=prop.get("google_maps_topo_url"),
                gis_url=prop.get("gis_url"),
                elevation_ft=prop.get("elevation_ft"),
                parcel_screenshot=prop.get("parcel_screenshot"),
                raw_source_text=prop.get("raw_source_text"),
                raw_parcel_text=prop.get("raw_parcel_text"),
                raw_deed_text=prop.get("raw_deed_text"),
                raw_paragraph=prop.get("raw_paragraph"),
                extracted_deed_plat=prop.get("extracted_deed_plat"),
                extracted_pin=prop.get("extracted_pin"),
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

            # Persist TNMap enrichment (owner, acres, gis_url, raw payload)
            # when the scraper produced it. These fields aren't part of the
            # base insert, so write them once we have the row id.
            if prop.get("tnmap_data"):
                try:
                    update_tnmap_enrichment(
                        conn, row["id"],
                        owner_name=prop.get("owner_name"),
                        acres=prop.get("acres"),
                        gis_url=prop.get("gis_url"),
                        google_maps_url=prop.get("google_maps_url"),
                        google_maps_topo_url=prop.get("google_maps_topo_url"),
                        tnmap_data=prop.get("tnmap_data"),
                    )
                except Exception as e:
                    logger.warning("TNMap persistence failed for #%s: %s", row["id"], e)

        except Exception as e:
            logger.error("Failed to save property: %s", e, exc_info=True)

    # Log scrape run completion
    _end_logging(conn, run_id, len(properties), new_count, dup_count, "completed")

    # Auto-enrich newly-found properties with NC OneMap GIS data
    try:
        from scraper.nc_gis_lookup import enrich_properties
        enrich_result = enrich_properties(source=scraper_name)
        if isinstance(enrich_result, dict):
            print(f"  Enriched: {enrich_result.get('enriched', 0)}, "
                  f"skipped(no parcel): {enrich_result.get('skipped_no_parcel', 0)}, "
                  f"failed: {enrich_result.get('failed', 0)}")
    except Exception as e:
        logger.warning("Auto-enrich failed: %s", e)

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


def _inc_failure_counter(scraper_name: str) -> None:
    """Increment consecutive failure count for a scraper."""
    logs_dir = config.logs_dir / "scraper_failures"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fpath = logs_dir / f"{scraper_name}.json"
    count = 0
    if fpath.exists():
        try:
            with open(fpath) as f:
                count = json.load(f).get("count", 0)
        except (ValueError, OSError):
            count = 0
    count += 1
    with open(fpath, "w") as f:
        json.dump({"count": count}, f)
    if count >= 3:
        logger.warning("%s failed %d consecutive runs — will be disabled", scraper_name, count)


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

     # Auto-archive properties with 0 < acres < MIN_ACRES
    print(f"\n{'='*60}")
    print(f"  Auto-archiving properties with 0 < acres < {config.MIN_ACRES:g} ...")
    conn = _ensure_db()
    try:
        archived = archive_below_acres(conn, min_acres=config.MIN_ACRES, include_sources=list(SCRAPER_MODULES.keys()))
        conn.close()
        if archived:
            print(f"  Auto-archived: {archived} properties")
        else:
            print(f"  No properties to auto-archive")
    except Exception as e:
        conn.close()
        logger.warning("Auto-archive failed: %s", e)

    # Auto-link properties appearing in both kania_law and nc_publicnotice
    try:
        conn = _ensure_db()
        try:
            from scraper import db as scraper_db
            link_result = scraper_db.link_cross_source(conn)
            if link_result.get("links"):
                print(f"  Cross-linked: {link_result.get('links')} pairs "
                      f"({link_result.get('notes')} notes)")
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Cross-link failed: %s", e)

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


def _next_run_time(hours: tuple[int, ...] = (4, 16)) -> datetime:
    """Return the next scheduled run datetime (local America/New_York)."""
    now = datetime.now()
    candidates = []
    for h in hours:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= now:
            t = t + timedelta(days=1)
        candidates.append(t)
    return min(candidates)


def cmd_cron(minutes: int = 720, hours: tuple[int, ...] = (4, 16)) -> None:
    """Run on a fixed daily schedule (default 4:00 AM & 4:00 PM America/New_York).

    `minutes` is kept for CLI compatibility but the schedule is driven by `hours`
    so scrapes land on exact wall-clock times regardless of start time.
    """
    os.environ.setdefault("TZ", "America/New_York")
    try:
        time.tzset()
    except Exception:
        pass
    schedule = ", ".join(f"{h:02d}:00" for h in hours)
    print(f"\n  CRON MODE: scheduled daily at {schedule} America/New_York (Ctrl+C to stop)")
    print(f"  Scrapers: {', '.join(sorted(SCRAPER_MODULES))}")
    print()

    while True:
        try:
            nxt = _next_run_time(hours)
            sleep_secs = (nxt - datetime.now()).total_seconds()
            print(f"  Next run scheduled for {nxt.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                  f"(in {sleep_secs / 3600:.1f}h)")
            time.sleep(max(sleep_secs, 1))
            print(f"\n  --- Starting scheduled scrape at "
                  f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')} ---")
            cmd_run_all()
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
        help="Run on a fixed daily schedule (4:00 AM & 4:00 PM America/New_York)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=360,
        help="Cron interval in minutes (default 360, kept for CLI compatibility)",
    )
    parser.add_argument(
        "--cron-hours",
        default="4,16",
        metavar="HH,HH",
        help="Cron schedule hours (comma-separated, 24h clock). Default '4,16' = 4 AM & 4 PM America/New_York",
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
    parser.add_argument(
        "--repair-links",
        action="store_true",
        help="Rebuild map/GIS links for all properties (fast: reuses stored coords)",
    )
    parser.add_argument(
        "--re-enrich",
        action="store_true",
        help="Re-enrich the entire list with corrected GIS logic (Cherokee via "
             "authoritative county GIS, others via NC OneMap)",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Make archive status consistent with current acreage "
             "(archive active < MIN_ACRES, unarchive archived >= MIN_ACRES)",
    )
    parser.add_argument(
        "--link-cross",
        action="store_true",
        help="Link properties that appear in both kania_law and nc_publicnotice "
             "(adds cross-source notes + links for the dashboard)",
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
    elif args.repair_links:
        from scraper.backfill_links import backfill_links
        result = backfill_links()
        print(f"\n  Link repair complete: {result.get('updated', 0)} updated, "
              f"{result.get('api_calls', 0)} API calls")
    elif args.enrich:
        result = cmd_enrich(args.enrich_source)
        if isinstance(result, dict):
            print(f"\n  Enrichment complete: {result.get('enriched', 0)} enriched, "
                  f"{result.get('skipped_no_parcel', 0)} skipped(no parcel), "
                  f"{result.get('failed', 0)} failed")
        else:
            print(f"\n  Enrichment complete: {result} updated")
    elif args.re_enrich:
        conn = _ensure_db()
        try:
            from scraper.nc_gis_lookup import re_enrich_all
            result = re_enrich_all(conn)
            print(f"\n  Re-enrich complete:")
            print(f"    Cherokee (county GIS): {result.get('cherokee')}")
            print(f"    NC OneMap fallback:    {result.get('nc_onemap_updated')}")
            print(f"    Skipped (manual lock): {result.get('skipped_locked')}")
            print(f"    Failed (no match):     {result.get('failed')}")
        finally:
            conn.close()
    elif args.reconcile:
        conn = _ensure_db()
        try:
            from scraper.nc_gis_lookup import reconcile_archive
            result = reconcile_archive(conn, args.threshold)
            print(f"\n  Reconcile complete: archived={result.get('archived')}, "
                  f"unarchived={result.get('unarchived')}")
        finally:
            conn.close()
    elif args.link_cross:
        conn = _ensure_db()
        try:
            from scraper import db as scraper_db
            result = scraper_db.link_cross_source(conn)
            print(f"\n  Cross-link complete: links={result.get('links')}, "
                  f"notes={result.get('notes')}")
        finally:
            conn.close()
    elif args.status:
        cmd_status()
    elif args.new:
        cmd_new()
    elif args.archive:
        cmd_archive(args.threshold)
    elif args.cron:
        hours = tuple(
            int(h) for h in args.cron_hours.split(",") if h.strip().isdigit()
        ) or (4, 16)
        cmd_cron(args.interval, hours=hours)
    elif args.scraper:
        cmd_run(args.scraper)
    elif args.all or not any([args.list, args.status, args.new, args.archive, args.cron, args.scraper]):
        cmd_run_all()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
