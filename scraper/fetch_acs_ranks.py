"""Build ``scraper/acs_zips.json`` — free rent/income ranks for GA zips.

Why: the RentCast plan is capped at ``config.RENTCAST_MONTHLY_BUDGET``
calls per billing window (50 on the current free plan) while the screen
universe is ~427 zips. Census ACS 5-year answers the same question for
free, for every ZCTA: median gross rent (B25064), median household income
(B19013) and housing units (B25002). RentCast then acts as the expensive
VERDICT layer, spent top-down on the ACS-ranked shortlist
(:func:`scraper.rent_market_scan.rank_queue`) instead of being wasted on
zip codes whose ACS median rent is $1,800 in the first place.

Where the data comes from (measured 2026-11-27):

  * ``api.census.gov`` now demands a free API key for EVERY request
    (single-ZCTA queries included — it answers 200 + a "Missing Key"
    HTML page). If ``CENSUS_API_KEY`` is set we use it: one documented
    wildcard request.
  * Otherwise the **undocumented** ``data.census.gov/api/access`` backend
    that powers the website itself answers key-free, exactly as a browser
    would see it. Verified against the rendered site: the backend's
    B25064 for ZCTA 30517 equals the figure the site displays (1,810).
    Because it is undocumented, the fetch VALIDATES the payload — median
    gross rents must be whole dollars in a sane band or the fetch fails
    loudly. An earlier probe that asked for B25062 (a *distribution*
    table) came back with plausible-looking garbage; that is the failure
    this guard exists for.

Downloaded through camoufox (project fetch policy). Rebuild yearly after
each ACS release::

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

logger = logging.getLogger(__name__)

OUT_PATH = os.path.join(os.path.dirname(__file__), "acs_zips.json")
REQUEST_TIMEOUT_MS = 120000
GA_STATE_FIPS = "13"

# ACS 5-year detailed tables: (key, variable, sane-estimate band).
# The band is the anti-garbage guard described in the module docstring.
TABLES = [
    ("rent", "B25064", (100, 5000)),    # median gross rent, $/month
    ("income", "B19013", (10_000, 250_000)),  # median household income
    ("units", "B25002", (0, 100_000)),  # total housing units (_001E; 0 is
                                        # a real answer for empty ZCTAs)
]
VINTAGES = ("2023", "2022", "2021")

# ACS negative sentinels for "no estimate" (-666666666 etc).
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


def api_url(vintage: str, tables) -> str:
    """Documented api.census.gov wildcard query (needs CENSUS_API_KEY)."""
    get = "NAME," + ",".join(f"{var}_001E" for _, var, _ in tables)
    return ("https://api.census.gov/data/{v}/acs/acs5?get={get}"
            "&for=zip%20code%20tabulation%20area:*&in=state:{st}"
            "&key={key}").format(v=vintage, get=get, st=GA_STATE_FIPS,
                                 key=os.environ.get("CENSUS_API_KEY", ""))


def backend_url(vintage: str, var: str) -> str:
    """Undocumented data.census.gov backend — what the site itself calls."""
    return ("https://data.census.gov/api/access/data/table"
            "?id=ACSDT5Y{v}.{var}&g=040XX00US{st}$8600000").format(
                v=vintage, var=var, st=GA_STATE_FIPS)


def parse_api_rows(rows) -> Dict[str, dict]:
    """[[header], [row]…] from api.census.gov -> zip -> metrics dict."""
    if not rows or not isinstance(rows, list) or not isinstance(rows[0], list):
        raise ValueError("unexpected census payload shape")
    header = [str(h).strip().lower() for h in rows[0]]

    def col(prefix: str) -> int:
        for i, h in enumerate(header):
            if h.startswith(prefix.lower()):
                return i
        raise ValueError(f"header lacks {prefix}: {header}")

    i_zip = len(header) - 1  # the geography column comes last
    out: Dict[str, dict] = {}
    for row in rows[1:]:
        zip_code = str(row[i_zip] or "").strip()
        if not (len(zip_code) == 5 and zip_code.isdigit()):
            continue
        entry = {}
        for key, var, _ in TABLES:
            try:
                entry[key] = _acs_int(row[col(var + "_001e")])
            except ValueError:
                entry[key] = None
        out[zip_code] = entry
    return out


def parse_backend_payload(payload: dict, var: str) -> Dict[str, int]:
    """{"response": {"data": [header, rows…]}} -> zip -> estimate int.

    Columns are located BY NAME (``<VAR>_001E``); the backend's column
    order is not part of any contract.
    """
    data = ((payload or {}).get("response") or {}).get("data") or []
    if len(data) < 2 or not isinstance(data[0], list):
        raise ValueError(f"backend {var}: unexpected payload shape")
    header = [str(h).strip() for h in data[0]]
    try:
        i_est = header.index(f"{var}_001E")
        i_geo = header.index("GEO_ID")
    except ValueError:
        raise ValueError(f"backend {var}: header lacks GEO_ID/{var}_001E: "
                         f"{header}")
    out: Dict[str, int] = {}
    for row in data[1:]:
        geo = str(row[i_geo] or "")
        zip_code = geo[-5:] if geo.startswith("860Z200US") else ""
        if len(zip_code) == 5 and zip_code.isdigit():
            value = _acs_int(row[i_est])
            if value is not None:
                out[zip_code] = value
    return out


def _validate(key: str, values: Dict[str, int], band: Tuple[int, int],
              required: bool) -> None:
    """Reject semantically-broken estimates BEFORE they rank anything."""
    lo, hi = band
    bad = [v for v in values.values() if not (lo <= v <= hi)]
    if len(bad) > max(2, 0.02 * len(values)):
        raise RuntimeError(
            f"{key}: {len(bad)}/{len(values)} estimates outside "
            f"${lo}-{hi} (worst {min(bad)}…{max(bad)}) — census changed "
            f"semantics; refusing to rank on garbage")
    if required and len(values) < 300:
        raise RuntimeError(f"{key}: only {len(values)} GA ZCTAs — expected "
                           f"~700; refusing a partial universe")


def _fetch_json(page, url: str):
    resp = page.request.get(url, timeout=REQUEST_TIMEOUT_MS)
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}")
    body = resp.body()
    if not body.lstrip()[:1] in (b"[", b"{"):
        raise RuntimeError(f"non-JSON response ({body[:60]!r})")
    return resp.json()


def download_acs(page) -> Tuple[str, Dict[str, dict]]:
    """(vintage, zip -> metrics) — documented API when keyed, else backend."""
    if os.environ.get("CENSUS_API_KEY"):
        for vintage in VINTAGES:
            try:
                rows = _fetch_json(page, api_url(vintage, TABLES))
                out = parse_api_rows(rows)
                _validate("rent", {z: e["rent"] for z, e in out.items()
                                   if e.get("rent")}, TABLES[0][2],
                          required=True)
                logger.info("api.census.gov %s: %d GA ZCTAs", vintage,
                            len(out))
                return vintage, out
            except Exception as e:  # noqa: BLE001 — try next vintage
                logger.warning("api.census.gov %s failed: %s", vintage, e)
        raise RuntimeError("keyed api.census.gov failed on every vintage")

    for vintage in VINTAGES:
        try:
            merged: Dict[str, dict] = {}
            for key, var, band in TABLES:
                payload = _fetch_json(page, backend_url(vintage, var))
                values = parse_backend_payload(payload, var)
                _validate(var, values, band, required=(key == "rent"))
                for zip_code, value in values.items():
                    merged.setdefault(zip_code, {})[key] = value
            for entry in merged.values():
                entry.setdefault("rent", None)
                entry.setdefault("income", None)
                entry.setdefault("units", None)
            logger.info("data.census.gov backend acs5 %s: %d GA ZCTAs",
                        vintage, len(merged))
            return vintage, merged
        except Exception as e:  # noqa: BLE001 — try next vintage
            logger.warning("backend acs5 %s failed: %s", vintage, e)
    raise RuntimeError("no census vintage fetched (see warnings)")


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
        "source": (f"Census ACS 5-year {vintage}: B25064 median gross rent, "
                   "B19013 median household income, B25002 housing units"),
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
