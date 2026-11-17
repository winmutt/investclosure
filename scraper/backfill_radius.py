"""Apply the distance rule (within ``config.RADIUS_MILES`` of the origin) to
existing rows.

The product is "ground at or above ``MIN_ELEVATION`` **and** inside the
radius". Elevation is backfilled by ``scraper.backfill_elevation``; this is
the other half. Rows are judged from their own coordinates when they have
them, and otherwise from their county's closest point to the origin in the
USGS boundary table (``scraper/county_radius.json``) — a county entirely
outside the radius takes its pin-less rows with it, while a county whose edge
reaches inside keeps them until a pin says otherwise.

Archiving is audited per row (``archive_reason='outside_radius (180 mi)'``)
and reversible:

    UPDATE properties SET status='active', archived_at=NULL,
                          archive_reason=NULL
    WHERE archive_reason LIKE 'outside_radius%'

Usage (inside the container)::

    python3 -m scraper.backfill_radius --dry-run          # report only
    python3 -m scraper.backfill_radius                    # archive
    python3 -m scraper.backfill_radius --source zillow    # one source
    python3 -m scraper.backfill_radius --radius 220       # what-if threshold

The same check runs automatically at the end of every scrape
(``run.run_scraper`` -> :func:`radius.apply_radius_rule`).
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from typing import Optional

from .config import config
from .db import _ensure_db
from .radius import apply_radius_rule, county_verdict, find_outside

logger = logging.getLogger(__name__)


def _report(conn: sqlite3.Connection, radius: float, hits: list,
            unknown: int) -> None:
    total = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE status='active'").fetchone()[0]
    print(f"\n  Origin {config.ORIGIN_LAT:.5f}, {config.ORIGIN_LON:.5f}"
          f"   radius {radius:g} mi")
    print(f"  Active rows {total}   outside {len(hits)}   "
          f"undecidable {unknown} (kept)")
    by_state = Counter(h["state"] or "?" for h in hits)
    by_basis = Counter(h["basis"] for h in hits)
    print(f"  Outside by state: {dict(sorted(by_state.items()))}")
    print(f"  Outside by basis: {dict(sorted(by_basis.items()))}"
          f"   ('county' = no pin and the whole county is out)")

    print("\n  Counties the reap comes from:")
    per = Counter((f"{(h['county'] or '?').title()}, {(h['state'] or '?')}"
                   ) for h in hits)
    for name, n in per.most_common(40):
        cty, _, st = name.partition(", ")
        verdict, mi = county_verdict(st.strip(), cty.strip())
        far = (f"county nearest point {mi:.0f} mi"
               if mi is not None and verdict == "out" else "mixed county")
        print(f"    {name:26s} {n:>4}  ({far})")

    print("\n  Sample rows:")
    for h in sorted(hits, key=lambda r: -(r["miles"] or 0))[:12]:
        print(f"    #{h['id']:<6} {h['county'] or '?'}, {h['state']} "
              f"{h['miles'] if h['miles'] is not None else '?'} mi"
              f"  [{h['basis']}]")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scraper.backfill_radius",
        description="Archive active properties outside the product radius.")
    parser.add_argument("--source", help="only this scraper source")
    parser.add_argument("--radius", type=float,
                        help="evaluate against this radius instead of the "
                             "configured RADIUS_MILES (the archive reason "
                             "records the number used)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only, change nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: "
                               "%(message)s")

    configured = config.RADIUS_MILES
    if args.radius is not None:
        config.RADIUS_MILES = args.radius
    conn = _ensure_db()
    try:
        hits, unknown = find_outside(conn, source=args.source)
        _report(conn, config.RADIUS_MILES, hits, unknown)
        stats = apply_radius_rule(conn, archive=not args.dry_run,
                                  source=args.source)
    finally:
        config.RADIUS_MILES = configured
        conn.close()

    if args.dry_run:
        print(f"\n  DRY RUN — nothing archived. "
              f"{stats['outside']} row(s) would go.")
    else:
        print(f"\n  Archived {stats['archived']} row(s).")
    if configured <= 0:
        print("  NOTE: RADIUS_MILES is 0 — the distance rule is disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
