"""GA tax-sale RENTALS scraper — georgiapublicnotice.com, rentals vertical.

The rentals product: inexpensive single-family homes bought at Georgia
delinquent-tax sheriff sales, in small towns within
``config.RENT_RADIUS_MILES`` of downtown Atlanta whose RentCast market data
shows a small house renting near $1,000/month (lower-middle-income rural
markets). Rows land on the **Rentals** vertical (``config.RENTAL_SOURCES``)
and are EXEMPT from the mountains parcel rules — no elevation, no
MIN_ACRES, no office-radius reap (see run.py's hook guard).

It rides the proven GA machinery (:class:`GAPublicNoticeScraper`): the
"Public Notice" ASP.NET grid per county, the Turnstile detail gate, the
bundled tax-sale per-parcel splitter. Three things change:

  * scope — counties come from the RentCast zip screen
    (:func:`scraper.rent_scope.target_counties`), not the mountain list;
  * category — "Tax Sales" only (no sheriff/security-deed mortgage sales);
  * classification — ``_is_mortgage_foreclosure`` is pinned False, so a
    bank power-of-sale notice drops as non-foreclosure instead of being
    kept for the Mtg tab, and every kept row is a tax sale.

Each row is stamped with ``market_rent_monthly`` — the county's screened
small-home rent — so tiles and the detail page show the rent thesis next
to the price. GA tax sales are held the first Tuesday of each month and
notices publish a few weeks ahead, so the twice-daily cron cadence of the
GA base scraper is already right here.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Set

from .config import GA_RENTAL_CATEGORIES
from .base import PropertyData
from .ga_publicnotice import GAPublicNoticeScraper
from . import rent_scope

logger = logging.getLogger(__name__)


class GATaxRentalsScraper(GAPublicNoticeScraper):
    """GA tax-sale scraper scoped to rentable small-home markets."""

    SOURCE_NAME = "ga_tax_rentals"
    RUN_TITLE = "GA TAX-SALE RENTALS"

    def __init__(self, *args, **kwargs):
        # Same residential-egress reasoning as the GA base (proxy off:
        # the IONOS datacenter exit trips Cloudflare; see ga_publicnotice).
        kwargs.setdefault("use_proxy", False)
        super().__init__(*args, **kwargs)

    # ---- scope -------------------------------------------------------------

    def _get_categories(self) -> str:
        """Tax sales only — the product is delinquent-tax sheriff sales."""
        return GA_RENTAL_CATEGORIES

    def _get_target_counties(self) -> Set[str]:
        """GA counties holding a RentCast-qualifying zip (see rent_scope).

        Empty scope is a data gap, not a quiet night: it means the fetch
        CLIs (fetch_ga_zips / fetch_county_radius / rent_market_scan) have
        not run, so say so loudly rather than pretending GA is calm.
        """
        counties = rent_scope.target_counties()
        if not counties:
            logger.error(
                "ga_tax_rentals has no county scope — %s. Run "
                "python3 -m scraper.rent_market_scan (needs "
                "RENTCAST_API_KEY), or the fetch CLIs listed in "
                "scraper/rent_scope.py.", rent_scope.describe_scope())
        return set(counties)

    # ---- tax-only policy -----------------------------------------------------

    @staticmethod
    def _is_mortgage_foreclosure(text: str) -> bool:
        """Always False here: the rentals vertical takes TAX sales only.

        With no mortgage signal recognized, a bank security-deed /
        power-of-sale notice falls through ``_extract_detail`` as
        ``dropped_non_foreclosure`` (honestly logged), and bundled tax-sale
        notices keep via ``_is_tax_foreclosure``.
        """
        return False

    def _apply_county_acreage_filter(self, properties: List[PropertyData],
                                     keep_unknown_acres: bool = True
                                     ) -> List[PropertyData]:
        """County filter WITHOUT the mountains MIN_ACRES floor.

        The rentals product is exactly the 0.4 ac town lot the mountains
        acreage rule discards; qualification there is the market screen
        (which picked the county) plus the tax notice itself.
        """
        state_counties = self._get_target_counties()
        filtered = []
        for prop in properties:
            county = (prop.get("county") or "").lower().strip().replace(" ", "_")
            if county and county in state_counties:
                prop["county"] = county.replace("_", " ").title()
                filtered.append(prop)
        print(f"  After filtering: {len(filtered)} qualifying, "
              f"{len(properties) - len(filtered)} skipped (county scope)")
        return filtered

    # ---- row decoration ------------------------------------------------------

    def _extract_detail(self, page, session_id: str,
                        record: dict) -> List[PropertyData]:
        """Parse the notice, then stamp the county's screened market rent."""
        props = super()._extract_detail(page, session_id, record)
        for p in props:
            rent = rent_scope.county_rent(p.get("county") or "")
            if rent:
                p["market_rent_monthly"] = rent
        return props


def scrape_with_enrichment(
    solve_captcha: bool = True,
    lookback_days: Optional[int] = None,
) -> List[PropertyData]:
    """Run the rentals scraper (GA has no statewide parcel enrichment)."""
    kwargs = {"solve_captcha": solve_captcha}
    if lookback_days is not None:
        kwargs["lookback_days"] = lookback_days
    scraper = GATaxRentalsScraper(**kwargs)
    return scraper.run()
