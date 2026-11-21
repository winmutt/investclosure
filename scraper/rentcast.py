"""RentCast API client — the rentability half of the rentals vertical.

RentCast (rentcast.io) answers two product questions:

  * "Which small towns can a cheap house rent for ~$1,000/mo?" — the
    ``/v1/markets`` zip endpoint gives aggregate sale + rental listing
    statistics (average/median, by bedrooms, listing counts, history).
  * "What would THIS house rent for?" — ``/v1/avm/rent/long-term`` per
    property AVM (kept for the per-row enrichment pass; zip-level market
    data is what scopes the scrape).

HTTP is curl_cffi — the same *scraper-code-path* exception the project
grants ``nc_gis_lookup`` / ``nconemap_net`` (api.rentcast.io is a key-auth
JSON API, not a browser-gated site). Interactive link checks still belong
in camoufox. Rate limit: 2 requests/second (project rule).

Auth: ``X-Api-Key`` header from ``config.RENTCAST_API_KEY``
(``RENTCAST_API_KEY`` env). Without a key every call raises — the market
scan CLI refuses politely so an empty key never looks like empty data.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from .config import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.rentcast.io/v1"
REQUEST_TIMEOUT_S = 30
MIN_INTERVAL_S = 0.55  # <= 2 requests/second, project-wide rate rule

_lock = threading.Lock()
_last_call = [0.0]


class RentCastError(RuntimeError):
    """Transport or API failure (non-404). 404 raises LookupError instead."""


def _throttle() -> None:
    with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def get_json(path: str, params: Dict[str, Any]) -> dict:
    """One authenticated GET against the RentCast API; JSON dict back."""
    key = (config.RENTCAST_API_KEY or "").strip()
    if not key:
        raise RentCastError(
            "RENTCAST_API_KEY is not set — add it to .env (rentcast.io/api)")
    from curl_cffi import requests as curl_requests
    url = f"{BASE_URL}{path}"
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        _throttle()
        try:
            resp = curl_requests.get(
                url, params=params, headers={"X-Api-Key": key},
                timeout=REQUEST_TIMEOUT_S, impersonate="chrome")
        except Exception as e:  # network / TLS
            last_err = e
            logger.warning("rentcast request error (%s) attempt %d: %s",
                           path, attempt, e)
            time.sleep(2.0)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            raise LookupError(f"no rentcast data for {params}")
        if resp.status_code == 401:
            raise RentCastError(
                "rentcast rejected the API key (401) — check "
                "RENTCAST_API_KEY in .env")
        last_err = RentCastError(
            f"HTTP {resp.status_code} from {path}: {resp.text[:200]}")
        if resp.status_code in (429, 500, 502, 503):
            logger.warning("rentcast %s attempt %d: HTTP %s — backing off",
                           path, attempt, resp.status_code)
            time.sleep(5.0)
            continue
        break
    raise last_err or RentCastError(f"rentcast request failed for {path}")


def summarize_market(payload: dict) -> Dict[str, Any]:
    """Reduce a /markets response to the fields the rentals screen needs.

    Pure function — no network, unit-testable against fixture payloads.
    Keeps current-month aggregates only (no history): small-home rent =
    the 3-bedroom (else 2-bedroom) rental median, sale cheapness =
    medianPrice, plus the listing counts that say whether to trust them.
    """
    out: Dict[str, Any] = {"zip": payload.get("zipCode")}
    rental = payload.get("rentalData") or {}
    sale = payload.get("saleData") or {}

    out["median_rent_all"] = rental.get("medianRent")
    out["average_rent_all"] = rental.get("averageRent")
    out["rent_listings"] = rental.get("totalListings")

    by_bd = {int(g.get("bedrooms") or 0): g
             for g in (rental.get("dataByBedrooms") or [])
             if isinstance(g, dict)}
    # Small uncomplicated family homes: 3 bd first, then 2 bd — the target
    # stock for a ~$1,000/mo mountain-foothill rental.
    for bd in (3, 2):
        g = by_bd.get(bd)
        if g and g.get("medianRent"):
            out["rent_small_home"] = g.get("medianRent")
            out["rent_basis"] = f"{bd}bd"
            out["rent_basis_listings"] = g.get("totalListings")
            break
    out["median_price"] = sale.get("medianPrice")
    out["average_price"] = sale.get("averagePrice")
    out["sale_listings"] = sale.get("totalListings")
    return out


def judge(summary: Dict[str, Any], min_rent: Optional[float] = None,
          max_rent: Optional[float] = None,
          min_listings: Optional[int] = None,
          max_median_price: Optional[int] = None) -> Dict[str, Any]:
    """Verdict for one zip: pass / borderline / fail / no_data.

    ``pass`` — the zip demonstrably supports renting a small home near the
    target band (config RENT_TARGET_MIN_RENT..MAX_RENT default $800–1,200)
    off enough listings, with an inexpensive sale market (median price at
    or under RENT_MAX_MEDIAN_PRICE) so a tax-sale buy stays cheap.
    ``borderline`` — right idea, weak evidence: rent just outside the band
    (±20%), too few listings, or sale prices above the cap but under 1.4x
    it. Borderline counties are scraped; the dashboard shows the numbers.
    ``no_data`` — no rental statistics at all (rural zips often have too
    few rental listings for RentCast to publish).
    """
    min_rent = (config.RENT_TARGET_MIN_RENT if min_rent is None
                else min_rent)
    max_rent = (config.RENT_TARGET_MAX_RENT if max_rent is None else max_rent)
    min_listings = (config.RENT_MIN_RENT_LISTINGS if min_listings is None
                    else min_listings)
    max_median_price = (config.RENT_MAX_MEDIAN_PRICE
                        if max_median_price is None else max_median_price)

    rent = summary.get("rent_small_home") or summary.get("median_rent_all")
    listings = (summary.get("rent_basis_listings")
                if summary.get("rent_small_home") else None)
    listings = (listings if listings is not None
                else summary.get("rent_listings"))
    price = summary.get("median_price")

    res = dict(summary, rent=rent, rent_listings_used=listings)
    if not rent:
        res["verdict"] = "no_data"
        return res

    thin = (listings or 0) < min_listings
    in_band = min_rent <= rent <= max_rent
    near_band = 0.8 * min_rent <= rent <= 1.2 * max_rent
    price_ok = price is None or price <= max_median_price
    price_near = price is not None and price <= 1.4 * max_median_price

    if in_band and price_ok and not thin:
        res["verdict"] = "pass"
    elif near_band and (price_ok or price_near):
        res["verdict"] = "borderline"
    else:
        res["verdict"] = "fail"
    # Gross-yield proxy on the median house — sorts the screen report
    # (a $950 rent against a $110k median is the thesis in one number).
    if price:
        res["gross_yield_pct"] = round(rent * 12 / price * 100, 1)
    return res


def market_for_zip(zip_code: str) -> Dict[str, Any]:
    """Fetch + summarize the current market for one zip."""
    payload = get_json("/markets",
                       {"zipCode": zip_code, "dataType": "All",
                        "historyRange": 3})
    return summarize_market(payload)


def rent_estimate(address: str) -> Optional[float]:
    """Per-property long-term rent AVM ($/month); None when unavailable.

    Used by the optional per-row enrichment once a row carries a full
    street address with ZIP (GA tax-sale notices often lack the ZIP, and
    the AVM degrades without it — zip-market data is the default signal).
    """
    try:
        data = get_json("/avm/rent/long-term", {"address": address})
    except (LookupError, RentCastError) as e:
        logger.info("no rent AVM for %r: %s", address, e)
        return None
    rent = data.get("averageRent") or data.get("longTermRent")
    try:
        return round(float(rent), 0) if rent else None
    except (TypeError, ValueError):
        return None
