"""Build ``scraper/acs_zips.json`` — free rent/income ranks for GA zips.

Why: the RentCast plan is capped at ``config.RENTCAST_MONTHLY_BUDGET``
calls per billing window (50 on the current free plan) while the screen
universe is ~427 zips. Census ACS 5-year answers the same question for
free, for every ZCTA, in ONE request: median gross rent (B25062), median
household income (B19013) and housing units (B25002). RentCast then acts
as the expensive VERDICT layer, spent top-down on the ACS-ranked shortlist
(:func:`scraper.rent_market_scan.rank_queue`) instead of being wasted on
zip codes whose ACS median rent is $1,800 in the first place.

Downloaded through camoufox (project fetch policy) from api.census.gov —
no key needed at this volume. Rebuild yearly after each ACS release::

    python3 -m scraper.fetch_acs_ranks
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date
from typing import Dict, Optional, Tuple

from .base import camoufox_context
from .fetch_ga_zips import TOWN_COUNTY_PATH  # noqa: F401 — same join source

logger = logging.getLogger(__name__)

OUT_PATH = os.path.join(os.path.dirname(__file__), "acs_zips.json")
REQUEST_TIMEOUT_MS = 120000

# ACS 5-year vintages with ZCTA support, newest first.
VINTAGES = ("2023", "2022", "2021", "2019")
VARIABLES = "NAME,B25062_001E,B19013_001E,B25002_001E"
GA_STATE_FIPS = "13"

# ACS publishes negative sentinels for "no estimate" (-666666666 etc).
_SENTINELS = {None, "", "null", "-666666666", "-222222222", "-111111111"}


def _acs_int(raw) -> Optional[int]:
    """ACS value -> int, or None for sentinels / negatives."""
    raw = (str(raw) if raw is not None else "").strip()
    if raw in _SENTINELS:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return value if value >= 0 else None


def acs_url(vintage: str) -> str:
    return ("https://api.census.gov/data/{v}/acs/acs5?get={vars}"
            "&for=zip%20code%20tabulation%20area:*&in=state:{st}").format(
                v=vintage, vars=VARIABLES, st=GA_STATE_FIPS)


def parse_acs_rows(rows) -> Dict[str, dict]:
    """[[header], [row]…] from the Census API -> zip -> metrics dict."""
    if not rows or not isinstance(rows, list) or not isinstance(rows[0], list):
        raise ValueError("unexpected census payload shape")
    header = [str(h).strip().lower() for h in rows[0]]

    def col(*names: str) -> int:
        for i, h in enumerate(header):
            if any(h.startswith(n) for n in names):
                return i
        raise ValueError(f"census header lacks any of {names}: {header}")

    i_rent = col("b25062_001e")
    i_inc = col("b19013_001e")
    i_units = col("b25002_001e")
    i_zip = len(header) - 1  # the geography column comes last
    out: Dict[str, dict] = {}
    for row in rows[1:]:
        zip_code = str(row[i_zip] or "").strip()
        if not (len(zip_code) == 5 and zip_code.isdigit()):
            continue
        out[zip_code] = {
            "rent": _acs_int(row[i_rent]),
            "income": _acs_int(row[i_inc]),
            "units": _acs_int(row[i_units]),
        }
    return out


def download_acs(page) -> Tuple[str, Dict[str, dict]]:
    """(vintage, zip -> metrics) from the first Census vintage that answers."""
    last_err: Optional[Exception] = None
    for vintage in VINTAGES:
        url = acs_url(vintage)
        try:
            resp = page.request.get(url, timeout=REQUEST_TIMEOUT_MS)
            if resp.status != 200:
                logger.warning("census %s -> HTTP %s", vintage, resp.status)
                last_err = RuntimeError(f"HTTP {resp.status}")
                continue
            rows = resp.json()
            parsed = parse_acs_rows(rows)
            if not parsed:
                last_err = RuntimeError("0 ZCTA rows")
                continue
            logger.info("census acs5 %s: %d GA ZCTAs", vintage, len(parsed))
            return vintage, parsed
        except Exception as e:  # noqa: BLE001 — try the next vintage
            last_err = e
            logger.warning("census acs5 %s failed: %s", vintage, e)
    raise RuntimeError(f"no census vintage fetched; last error: {last_err}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build scraper/acs_zips.json (free ACS rent ranks "
                    "for the capped RentCast screen).")
    parser.add_argument("--out", default=OUT_PATH)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: "
                               "%(message)s")
    with camoufox_context() as page:
        vintage, zips = download_acs(page)

    payload = {
        "generated": date.today().isoformat(),
        "source": (f"Census ACS 5-year {vintage} via api.census.gov: "
                   "B25062_001E median gross rent, B19013_001E median "
                   "household income, B25002_001E housing units"),
        "vintage": vintage,
        "zips": zips,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    with_rent = sum(1 for z in zips.values() if z.get("rent"))
    print(f"wrote {args.out}: {len(zips)} GA ZCTAs "
          f"({with_rent} with a median-rent estimate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
