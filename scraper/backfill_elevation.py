"""Backfill per-parcel ground elevation (USGS 3DEP) and apply the parcel rule.

The qualifying county lists only encode each county's **peak** elevation
(``config.QUALIFYING_COUNTIES``, >=1700ft), so a parcel in a valley inside a
qualifying county passed every existing filter. A live USGS sample of active
rows measured **70% below 1,700 ft** (all SC/AL, most GA/TN foothills), and
``properties.elevation_ft`` was NULL on every row — the per-property
``MIN_ELEVATION`` filter that the legacy realestate app had was never ported.

This CLI fills ``elevation_ft`` for every active row with coordinates and,
unless ``--no-archive`` is given, archives the ones below
``config.MIN_ELEVATION`` (audited, ``archive_reason='below_min_elevation
(1700 ft)'``). Unknown elevation stays NULL and is never disqualifying.

Usage (inside the container)::

    python3 -m scraper.backfill_elevation --dry-run        # measure + report
    python3 -m scraper.backfill_elevation --limit 200      # small live pass
    python3 -m scraper.backfill_elevation --no-archive     # fill, keep rows
    python3 -m scraper.backfill_elevation --source zillow  # one source
    python3 -m scraper.backfill_elevation --refresh         # re-measure all

The same enrichment runs automatically for new rows at the end of every
scrape (``run.run_scraper`` -> :func:`elevation.enrich_elevations`).
"""
from __future__ import annotations

import argparse
import logging

from .config import config
from .db import _ensure_db
from .elevation import enrich_elevations

logger = logging.getLogger(__name__)


def _print_stats(stats: dict, dry_run: bool) -> None:
    min_elev = stats["min_elevation"]
    print(f"\n  Elevation target: {stats['target']} row(s) "
          f"(rule: >= {min_elev:.0f} ft)" if min_elev > 0
          else f"\n  Elevation target: {stats['target']} row(s) (rule disabled)")
    print(f"  Measured: {stats['measured']}  "
          f"unknown: {stats['unknown']}")
    if dry_run:
        print(f"  Below {min_elev:.0f} ft (would archive): {stats['below']}")
    else:
        print(f"  Below {min_elev:.0f} ft: {stats['below']}  "
              f"archived: {stats['archived']}")
    if stats["by_state"]:
        print("  By state (measured / below):")
        for state in sorted(stats["by_state"]):
            bucket = stats["by_state"][state]
            print(f"    {state:<3} {bucket['measured']:>5} / {bucket['below']:>5}")

    if not dry_run and stats["measured"]:
        conn = _ensure_db()
        try:
            never = conn.execute(
                "SELECT COUNT(*) FROM properties WHERE status='active' "
                "AND elevation_ft IS NULL").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM properties WHERE status='active'"
            ).fetchone()[0]
            print(f"  Active rows still without elevation: {never} "
                  f"(of {active})")
            if never:
                print("  (rows with no coordinates can never be measured; "
                      "run the GIS enricher first)")
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scraper.backfill_elevation",
        description="Fill properties.elevation_ft from USGS 3DEP and apply "
                    "the per-parcel MIN_ELEVATION rule.")
    parser.add_argument("--source", help="only this scraper source")
    parser.add_argument("--limit", type=int,
                        help="measure at most N rows")
    parser.add_argument("--no-archive", action="store_true",
                        help="fill elevation but archive nothing")
    parser.add_argument("--refresh", action="store_true",
                        help="re-measure rows that already have an elevation")
    parser.add_argument("--dry-run", action="store_true",
                        help="measure and report only (no DB writes)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    conn = _ensure_db()
    try:
        stats = enrich_elevations(
            source=args.source,
            limit=args.limit,
            only_missing=not args.refresh,
            archive=not args.no_archive,
            write=not args.dry_run,
            conn=conn,
        )
    finally:
        conn.close()
    _print_stats(stats, args.dry_run)
    if config.MIN_ELEVATION <= 0:
        print("  NOTE: MIN_ELEVATION is 0 — the parcel rule is disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
