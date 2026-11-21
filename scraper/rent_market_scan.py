"""RentCast zip-market screen for the rentals vertical.

Answers "which GA small towns support renting an inexpensive house near
$1,000/mo?" — walks the in-radius GA zips (``scraper/ga_zips.json``),
fetches each zip's RentCast /markets aggregate, judges it
(:func:`scraper.rentcast.judge`), and caches verdicts to
``scraper/rent_markets.json``. The scrape scope
(:func:`scraper.rent_scope.target_counties`) reads that file, and the
dashboard tiles show the stamped ``market_rent_monthly``.

Usage (container)::

    python3 -m scraper.rent_market_scan            # fetch what is stale
    python3 -m scraper.rent_market_scan --refresh  # refetch everything
    python3 -m scraper.rent_market_scan --limit 25 --county fulton
    python3 -m scraper.rent_market_scan --report-only   # no API calls

Refresh cadence: monthly is plenty (the underlying listing aggregates move
slowly, and each pass costs ~one call per zip). Verdicts checkpoint after
every fetch, so an interrupted run resumes where it stopped.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from .config import config
from .rent_scope import ZIPS_PATH
from .rentcast import RentCastError, judge, market_for_zip

logger = logging.getLogger(__name__)

MARKETS_PATH = os.path.join(os.path.dirname(__file__), "rent_markets.json")
REPORT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "reports", "rent_markets.md")
REFRESH_DAYS = 28
CHECKPOINT_EVERY = 10

_VERDICT_RANK = {"pass": 0, "borderline": 1, "no_data": 2, "fail": 3}


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.error("cannot read %s: %s", path, e)
        return {}


def _save_markets(payload: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _is_fresh(entry: dict, refresh_days: int) -> bool:
    fetched = (entry or {}).get("fetched") or ""
    try:
        when = datetime.fromisoformat(fetched)
    except ValueError:
        return False
    return when > datetime.now() - timedelta(days=refresh_days)


def screen(zips: Dict[str, dict], existing: Dict[str, dict], *,
           refresh: bool, limit: Optional[int], only: Optional[set],
           only_county: Optional[str], refresh_days: int,
           on_progress) -> Dict[str, dict]:
    """Fetch + judge each queued zip; ``on_progress`` checkpoints results."""
    today = date.today().isoformat()
    queued = []
    for zip_code, meta in sorted(zips.items()):
        if only and zip_code not in only:
            continue
        if only_county and only_county not in [c.lower()
                                               for c in meta["counties"]]:
            continue
        if not refresh and _is_fresh(existing.get(zip_code), refresh_days):
            continue
        queued.append(zip_code)
    if limit:
        queued = queued[:limit]
    logger.info("%d zips queued (%d cached fresh)", len(queued),
                len(zips) - len(queued))

    done = 0
    for zip_code in queued:
        meta = zips[zip_code]
        try:
            summary = market_for_zip(zip_code)
            entry = judge(summary)
        except LookupError:
            entry = {"verdict": "no_data", "note": "rentcast has no market"}
        except RentCastError as e:
            logger.error("rentcast fetch failed for %s: %s — stopping pass",
                         zip_code, e)
            break
        entry.update(counties=meta["counties"], mi_atl=meta.get("mi_atl"),
                     fetched=today)
        existing[zip_code] = entry
        done += 1
        on_progress(done, len(queued), zip_code, entry)
    return existing


def write_report(markets: Dict[str, dict], path: str) -> None:
    """Markdown screen report — pass rows first, best yield first."""
    rows = sorted(
        markets.items(),
        key=lambda kv: (_VERDICT_RANK.get(kv[1].get("verdict"), 9),
                        -(kv[1].get("gross_yield_pct") or 0)))
    counts: Dict[str, int] = {}
    for _, e in rows:
        counts[e.get("verdict", "?")] = counts.get(e.get("verdict", "?"), 0) + 1
    lines = [
        "# GA rental-market screen (RentCast /markets)",
        "",
        f"Generated {date.today().isoformat}. Band "
        f"${config.RENT_TARGET_MIN_RENT:.0f}–{config.RENT_TARGET_MAX_RENT:.0f}/mo "
        f"small-home rent, ≥{config.RENT_MIN_RENT_LISTINGS} rental listings, "
        f"median sale ≤ ${config.RENT_MAX_MEDIAN_PRICE:,}. "
        f"Verdicts: " + ", ".join(f"{v} {n}" for v, n in sorted(counts.items())),
        "",
        "| Zip | Counties | mi ATL | Rent (basis) | Listings | Median sale | Yield | Verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for zip_code, e in rows:
        rent = e.get("rent")
        basis = e.get("rent_basis") or "all"
        price = e.get("median_price")
        lines.append(
            f"| {zip_code} | {', '.join(e.get('counties') or [])} "
            f"| {e.get('mi_atl', '')} "
            f"| {'$' + format(int(rent), ',') if rent else '—'} ({basis}) "
            f"| {e.get('rent_listings_used') or 0} "
            f"| {'$' + format(int(price), ',') if price else '—'} "
            f"| {e.get('gross_yield_pct') or ''} "
            f"| {e.get('verdict', '')} |")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    logger.info("report written to %s", path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RentCast zip-market screen (rentals vertical scope).")
    parser.add_argument("--refresh", action="store_true",
                        help=f"refetch every zip (else skip zips fetched "
                             f"within {REFRESH_DAYS} days)")
    parser.add_argument("--refresh-days", type=int, default=REFRESH_DAYS)
    parser.add_argument("--limit", type=int, help="cap fetches this run")
    parser.add_argument("--zips", help="comma-separated zip allowlist")
    parser.add_argument("--county", help="only zips touching this county")
    parser.add_argument("--report-only", action="store_true",
                        help="regenerate the report from cached verdicts")
    parser.add_argument("--markets-path", default=MARKETS_PATH)
    parser.add_argument("--report-path", default=REPORT_PATH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: "
                               "%(message)s")

    zips_data = _load(ZIPS_PATH)
    zips = zips_data.get("zips") or {}
    if not zips:
        print("no zip universe — run python3 -m scraper.fetch_ga_zips first",
              file=sys.stderr)
        return 2

    markets_doc = _load(args.markets_path)
    markets: Dict[str, dict] = markets_doc.get("zips") or {}

    if args.report_only:
        write_report(markets, args.report_path)
        return 0

    if not (config.RENTCAST_API_KEY or "").strip():
        print("RENTCAST_API_KEY is not set — add it to .env "
              "(get one at rentcast.io/api) and rerun.", file=sys.stderr)
        return 2

    def checkpoint(done: int, total: int, zip_code: str, entry: dict) -> None:
        if done % 25 == 0 or done == total:
            print(f"  [{done}/{total}] {zip_code}: "
                  f"${entry.get('rent') or 0:.0f} {entry.get('verdict')}")
        if done % CHECKPOINT_EVERY == 0:
            _save_markets({**markets_doc, "zips": markets}, args.markets_path)

    markets = screen(zips, markets, refresh=args.refresh, limit=args.limit,
                     only=set((args.zips or "").split(",")) - {""},
                     only_county=(args.county or "").lower() or None,
                     refresh_days=args.refresh_days,
                     on_progress=checkpoint)

    _save_markets({
        "generated": date.today().isoformat(),
        "source": "RentCast /v1/markets per zip",
        "screen": {
            "rent_min": config.RENT_TARGET_MIN_RENT,
            "rent_max": config.RENT_TARGET_MAX_RENT,
            "min_rent_listings": config.RENT_MIN_RENT_LISTINGS,
            "max_median_price": config.RENT_MAX_MEDIAN_PRICE,
        },
        "zips": markets,
    }, args.markets_path)
    write_report(markets, args.report_path)

    from .rent_scope import describe_scope
    print(f"\n{describe_scope()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
