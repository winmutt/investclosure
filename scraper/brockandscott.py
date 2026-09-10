"""Brock & Scott trustee-sale search scraper (mortgage foreclosures).

https://www.brockandscott.com/search-foreclosure-sales/ searches by state
(NC/GA/TN among others) and lands on a parameterized results URL
(/foreclosure-sales/?_sft_foreclosure_state=nc) with a 20-per-page table:
COUNTY | SALE DATE | STATE | COURT SP# | CASE# | ADDRESS | OPENING BID |
BOOK PAGE. Paginated to exhaustion per state, filtered to our mountain
county sets; every row is a mortgage/deed-of-trust sale for the Mtg tab.
"""
from __future__ import annotations
import logging
import time
from datetime import date
from typing import Dict, List, Optional

from .base import PropertyData, camoufox_context
from .config import (
    GA_MOUNTAIN_COUNTIES,
    NC_MOUNTAIN_COUNTIES,
    TN_FORECLOSURE_COUNTIES,
)
from .trustee_base import (
    TrusteeSaleScraper, clean_html, parse_sale_date, price_to_dollars,
    read_html_table, split_address,
)

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.brockandscott.com/foreclosure-sales/"
STATE_NAMES = {"NC": "North Carolina", "GA": "Georgia", "TN": "Tennessee"}
STATE_COUNTIES: Dict[str, set[str]] = {
    "NC": set(NC_MOUNTAIN_COUNTIES),
    "GA": set(GA_MOUNTAIN_COUNTIES),
    "TN": set(TN_FORECLOSURE_COUNTIES),
}
MAX_PAGES_PER_STATE = 30


class BrockScottScraper(TrusteeSaleScraper):
    SOURCE_NAME = "brockandscott"
    STATE = ""  # multi-state; set per row
    TARGET_COUNTIES = set()  # per-state via STATE_COUNTIES
    LIST_URL = "https://www.brockandscott.com/search-foreclosure-sales/"

    def scrape(self) -> List[PropertyData]:
        props: List[PropertyData] = []
        try:
            with camoufox_context() as page:
                for state in ("NC", "GA", "TN"):
                    props.extend(self._scrape_state(page, state))
        except Exception as e:
            logger.error("BrockScott fetch failed: %s", e, exc_info=True)
        return props

    # -- per-state pagination --------------------------------------

    def _parse_cells(self, cells, state: str, state_name: str,
                       counties: set, url: str, today: str):
        """Parse one results-table row -> (PropertyData|None, dropped bool)."""
        if len(cells) < 8:
            return None, False
        county, sale_raw, st, court_sp, case_no, addr, bid, book = (
            clean_html(c) for c in cells[:8])
        if county.lower() == "county" or not case_no:
            return None, False  # header row
        if county.strip().lower() not in counties:
            return None, True
        sale_date = parse_sale_date(sale_raw)
        if sale_date is not None and sale_date < today:
            return None, True
        street, city, zip_code = split_address(addr, state_name)
        desc = (f"Sale: {sale_raw} | Case: {case_no} | Court SP: {court_sp} | "
                f"{addr} | Opening bid: {bid} | Deed: {book}")
        return self.build_property(
            listing_id=f"{state.lower()}:{case_no}",
            county=county,
            state=state,
            address=street,
            city=city,
            zip_code=zip_code,
            court_case=court_sp or None,
            sale_date=sale_date,
            price_dollars=price_to_dollars(bid),
            description=desc,
            raw_text=desc,
            url=url,
        ), False

    def _pager_hrefs(self, page) -> List[str]:
        try:
            return page.evaluate("""() => Array.from(document.querySelectorAll('a'))
              .map(a => ({t: (a.innerText || '').trim(), h: a.href || ''}))
              .filter(x => /^(\\d+|Next|›|»)$/.test(x.t) && x.h)
              .map(x => x.h)""") or []
        except Exception:
            return []

    def _load(self, page, url: str) -> bool:
        for attempt in range(3):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
                return True
            except Exception as e:
                logger.warning("B&S load failed (attempt %d): %s", attempt + 1, e)
                time.sleep(5)
        return False

    def _scrape_state(self, page, state: str) -> List[PropertyData]:
        today = date.today().isoformat()
        counties = STATE_COUNTIES[state]
        state_name = STATE_NAMES[state]
        props: List[PropertyData] = []
        seen_urls: set[str] = set()
        url = f"{SEARCH_URL}?_sft_foreclosure_state={state.lower()}"
        kept = skipped = 0
        for _ in range(MAX_PAGES_PER_STATE):
            if url in seen_urls:
                break
            seen_urls.add(url)
            if not self._load(page, url):
                break
            for cells in read_html_table(page, 0):
                prop, dropped = self._parse_cells(
                    cells, state, state_name, counties, url, today)
                if prop:
                    props.append(prop)
                    kept += 1
                elif dropped:
                    skipped += 1
            nxt = next((h for h in self._pager_hrefs(page) if h not in seen_urls), None)
            if not nxt:
                break
            url = nxt
            time.sleep(2)
        logger.info("B&S %s: %d kept, %d skipped over %d pages",
                    state, kept, skipped, len(seen_urls))
        return props


def scrape_all() -> List[PropertyData]:
    return BrockScottScraper().scrape()
