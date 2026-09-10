"""Rubin Lublin GA trustee-sale listings scraper (mortgage foreclosures).

https://rlselaw.com/property-listing/georgia-property-listings/ renders a
server-side sortable table: Sale Date | File # | Property | City | Zip |
County | Bid (270+ rows statewide). Filtered to the 7 N GA mountain
counties; every row is a mortgage/deed-of-trust sale for the Mtg tab.
"""
from __future__ import annotations
import logging
import time
from datetime import date
from typing import List

from .base import PropertyData, camoufox_context
from .config import GA_MOUNTAIN_COUNTIES
from .trustee_base import (
    TrusteeSaleScraper, clean_html, parse_sale_date, price_to_dollars,
    read_html_table,
)

logger = logging.getLogger(__name__)

LIST_URL = "https://rlselaw.com/property-listing/georgia-property-listings/"


class RLSelawScraper(TrusteeSaleScraper):
    SOURCE_NAME = "rlselaw"
    STATE = "GA"
    TARGET_COUNTIES = set(GA_MOUNTAIN_COUNTIES)
    LIST_URL = LIST_URL

    def scrape(self) -> List[PropertyData]:
        today = date.today().isoformat()
        props: List[PropertyData] = []
        kept = skipped_county = skipped_past = 0
        try:
            with camoufox_context() as page:
                page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
                rows = read_html_table(page, 0)
        except Exception as e:
            logger.error("RLSelaw fetch failed: %s", e, exc_info=True)
            return []
        logger.info("RLSelaw: %d table rows", len(rows))
        for cells in rows:
            prop, reason = self._parse_cells(cells, today)
            if prop:
                props.append(prop)
                kept += 1
            elif reason == "county":
                skipped_county += 1
            elif reason == "past":
                skipped_past += 1
            time.sleep(0.5)
        logger.info("RLSelaw: %d kept, %d non-target county, %d past sale",
                    kept, skipped_county, skipped_past)
        return props

    def _parse_cells(self, cells, today: str):
        """Parse one table row -> (PropertyData|None, skip-reason|None)."""
        if len(cells) < 7:
            return None, "short"
        sale_raw, file_no, street, city, zip_code, county, bid = (
            clean_html(cells[0]), clean_html(cells[1]), clean_html(cells[2]),
            clean_html(cells[3]), clean_html(cells[4]), clean_html(cells[5]),
            clean_html(cells[6]),
        )
        if sale_raw.lower().startswith("sale date") or not file_no:
            return None, "header"
        if not self.keep_county(county):
            return None, "county"
        sale_date = parse_sale_date(sale_raw)
        if sale_date is not None and sale_date < today:
            return None, "past"
        desc = (f"Sale: {sale_raw} | File #: {file_no} | {street}, "
                f"{city} {zip_code} | {county} County, GA | Bid: {bid}")
        return self.build_property(
            listing_id=file_no,
            county=county,
            address=street or None,
            city=city or None,
            zip_code=zip_code or None,
            sale_date=sale_date,
            price_dollars=price_to_dollars(bid),
            description=desc,
            raw_text=desc,
        ), None


def scrape_all() -> List[PropertyData]:
    return RLSelawScraper().scrape()
