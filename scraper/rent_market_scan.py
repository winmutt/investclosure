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

Budget: the plan allows ``config.RENTCAST_MONTHLY_BUDGET`` calls per
monthly window (50 today) while the universe is ~427 zips — so the queue
is ordered by the FREE Census ranks (``scraper/acs_zips.json``, see
:mod:`scraper.fetch_acs_ranks`): in-band median gross rent first, thin
stock and out-of-band markets last. The client's ledger (:func:`scraper.
rentcast.budget_status`) then hard-stops the walk at the cap; whatever
remains queued waits for the next window (verdicts are cached, so each
month the walk continues down the list).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from .config import config
from .rent_scope import ZIPS_PATH
from .rentcast import (BudgetExhausted, RentCastError, budget_status, judge,
                       market_for_zip)

logger = logging.getLogger(__name__)

MARKETS_PATH = os.path.join(os.path.dirname(__file__), "rent_markets.json")
ACS_PATH = os.path.join(os.path.dirname(__file__), "acs_zips.json")
REPORT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "reports", "rent_markets.md")
REFRESH_DAYS = 28
CHECKPOINT_EVERY = 10
# ZCTAs under this many housing units rarely hold RENT_MIN_RENT_LISTINGS
# rental listings — RentCast would spend a paid call answering no_data.
MIN_UNITS = 300

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


def rank_key(zip_code: str, zips: Dict[str, dict], acs: Dict[str, dict],
             rent_min: float, rent_max: float) -> tuple:
    """Queue order for a capped budget: best RentCast-ROI zip first.

    Tier 0..2 — ACS median gross rent in the target band, in the ±20%
    near-band, or further out; tier 3..5 the same three with thin housing
    stock (< ``MIN_UNITS`` — RentCast would likely answer no_data, but
    unknown stock counts as fine); tier 6 — no ACS estimate, ordered
    nearest-Atlanta-first (closer zips tend to have listing coverage).
    Within a tier, ascending distance from the band, then bigger stock.
    """
    entry = acs.get(zip_code) or {}
    rent = entry.get("rent")
    units = entry.get("units") or 0
    mi = (zips.get(zip_code) or {}).get("mi_atl")
    mi = 999.0 if mi is None else float(mi)
    if not rent:
        return (6, 0, 0, mi, zip_code)
    in_band = rent_min <= rent <= rent_max
    near = 0.8 * rent_min <= rent <= 1.2 * rent_max
    band_tier = 0 if in_band else (1 if near else 2)
    thin = 0 < units < MIN_UNITS
    dist = (0 if in_band
            else min(abs(rent - rent_min), abs(rent - rent_max)))
    return (band_tier + (3 if thin else 0), dist, -units, mi, zip_code)


def rank_queue(queued: List[str], zips: Dict[str, dict],
               acs: Dict[str, dict], *,
               rent_min: Optional[float] = None,
               rent_max: Optional[float] = None) -> List[str]:
    """Order the queue by :func:`rank_key` (band default from config)."""
    rent_min = (config.RENT_TARGET_MIN_RENT if rent_min is None
                else rent_min)
    rent_max = (config.RENT_TARGET_MAX_RENT if rent_max is None
                else rent_max)
    return sorted(queued,
                  key=lambda z: rank_key(z, zips, acs, rent_min, rent_max))


def screen(zips: Dict[str, dict], existing: Dict[str, dict], *,
           refresh: bool, limit: Optional[int], only: Optional[set],
           only_county: Optional[str], refresh_days: int,
           on_progress, acs: Optional[Dict[str, dict]] = None,
           ) -> Dict[str, dict]:
    """Fetch + judge each queued zip; ``on_progress`` checkpoints results.

    With ``acs`` the queue is walked best-ACS-rank first, so a budget
    stop (``BudgetExhausted``) spends the cap where the payoff is.
    """
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
    if acs:
        queued = rank_queue(queued, zips, acs)
    if limit:
        queued = queued[:limit]

    status = budget_status()
    logger.info("%d zips queued (%d cached fresh)", len(queued),
                len(zips) - len(queued))
    if status["budget"]:
        logger.info("rentcast budget: %d/%d calls used since %s — %d left",
                    status["calls"], status["budget"],
                    status["window_start"], status["remaining"])
        if len(queued) > status["remaining"]:
            logger.warning(
                "queue exceeds the remaining monthly budget — walking in "
                "rank order and stopping at the cap; the rest waits for "
                "the next window")

    done = 0
    for zip_code in queued:
        meta = zips[zip_code]
        try:
            summary = market_for_zip(zip_code)
            entry = judge(summary)
        except LookupError:
            entry = {"verdict": "no_data", "note": "rentcast has no market"}
        except BudgetExhausted as e:
            logger.warning("%s — pass stops here", e)
            break
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
    parser.add_argument("--acs-path", default=ACS_PATH)
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

    status = budget_status()
    if status["budget"]:
        print(f"RentCast budget: {status['calls']}/{status['budget']} calls "
              f"used since {status['window_start']} — {status['remaining']} "
              f"left this window")
    acs = _load(args.acs_path).get("zips") or {}
    if not acs:
        logger.warning("no ACS ranks (%s) — queue stays alphabetical; run "
                       "python3 -m scraper.fetch_acs_ranks so the capped "
                       "budget goes to the best zips first", args.acs_path)

    def checkpoint(done: int, total: int, zip_code: str, entry: dict) -> None:
        if done % 10 == 0 or done == total:
            print(f"  [{done}/{total}] {zip_code}: "
                  f"${entry.get('rent') or 0:.0f} {entry.get('verdict')}")
        if done % CHECKPOINT_EVERY == 0:
            _save_markets({**markets_doc, "zips": markets}, args.markets_path)

    markets = screen(zips, markets, refresh=args.refresh, limit=args.limit,
                     only=set((args.zips or "").split(",")) - {""},
                     only_county=(args.county or "").lower() or None,
                     refresh_days=args.refresh_days,
                     on_progress=checkpoint, acs=acs or None)

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
    status = budget_status()
    if status["budget"]:
        print(f"RentCast budget after run: {status['calls']}/{status['budget']}"
              f" — {status['remaining']} left until "
              f"{status['window_start']} + 1 month")
    print(f"\n{describe_scope()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
