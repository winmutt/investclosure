"""Macon County (NC) tax foreclosure monitor.

The county page (https://maconnc.org/maconcountyforeclosures.html) is a Tax
Department FAQ page — no parcel rows, no sale table. This scraper is a
monitor: it scans the page for parcel-bearing sale content (tables or
foreclosure links with parcel/case tokens) and emits rows if any appear;
otherwise it logs kept_empty and returns []. When the county posts a live
sale list the tables parser picks it up with no code change.

No captcha, no Turnstile.
"""
from __future__ import annotations

import logging

from .base import BaseScraper, PropertyData
from . import county_static as cs
from .rawlog import log_raw

logger = logging.getLogger(__name__)

PAGE_URL = "https://maconnc.org/maconcountyforeclosures.html"
COUNTY = "Macon"


class MaconCountyScraper(BaseScraper):
    """Monitor the Macon County foreclosure page for parcel-level sale content."""

    SOURCE_NAME = "macon_county"
    BASE_URL = PAGE_URL

    def __init__(self, delay_range: tuple[float, float] = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)

    def scrape(self) -> list[PropertyData]:
        logger.info("Checking Macon County foreclosure page ...")
        try:
            tables = cs.extract_tables(PAGE_URL)
            page = cs.fetch_page(PAGE_URL)
        except Exception as e:
            logger.error("Macon County fetch failed: %s", e)
            return []
        body = page.get("body") or ""

        properties: list[PropertyData] = []
        for table in tables:
            for row in table["rows"]:
                blob = " | ".join(row)
                parcels = cs.find_parcels(blob)
                if not parcels:
                    continue
                for parcel in parcels:
                    lid = f"macon_{parcel}_{cs.content_hash(blob)[:8]}"
                    price = cs.parse_money(blob)
                    sale_iso = cs.to_iso_date(blob)
                    case = cs.find_case(blob)
                    log_raw(
                        self.SOURCE_NAME, listing_id=lid, county=COUNTY,
                        state="NC", decision="kept_tax",
                        reason="parcel row in county table",
                        raw_text=blob, url=PAGE_URL,
                    )
                    properties.append(cs.build_tax_row(
                        source=self.SOURCE_NAME, listing_id=lid, url=PAGE_URL,
                        county=COUNTY, parcel=parcel, price=price,
                        auction_date=sale_iso, court_case=case,
                        description=f"[Macon Co] tax sale parcel {parcel}",
                        raw_text=blob, extra_key="table",
                    ))

        if not properties:
            cs.log_kept_empty(
                self.SOURCE_NAME, COUNTY,
                "FAQ page, no parcel-level sale content", body[:2000], PAGE_URL,
            )
            logger.info("Macon County: no sale content (FAQ only)")
        else:
            logger.info("Macon County: %d parcel rows", len(properties))
        return properties
