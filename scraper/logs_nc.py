"""LOGS NC trustee-sale scraper (mortgage foreclosures).

https://www.logs.com/nc-upcoming-sales-report.html embeds a PowerBI
"Upcoming Sales NC" report (default slicer: next 10 days). The table visual
renders as DOM: County Name | Sale Date | Sale Time | Case Court Numb |
Full Address | Bid Amnt. Rows are virtualized, so the scraper scrolls the
visual until no new rows materialize, then filters to the 21 NC mountain
counties. Every row is a mortgage/deed-of-trust sale for the Mtg tab.
"""
from __future__ import annotations
import logging
import time
from datetime import date
from typing import Dict, List, Optional

from .base import PropertyData, camoufox_context
from .config import NC_MOUNTAIN_COUNTIES
from .trustee_base import (
    TrusteeSaleScraper, clean_html, parse_sale_date, price_to_dollars,
    split_address,
)

logger = logging.getLogger(__name__)

LIST_URL = "https://www.logs.com/nc-upcoming-sales-report.html"
MAX_SCROLLS = 40


class LogsNCScraper(TrusteeSaleScraper):
    SOURCE_NAME = "logs_nc"
    STATE = "NC"
    TARGET_COUNTIES = set(NC_MOUNTAIN_COUNTIES)
    LIST_URL = LIST_URL

    def scrape(self) -> List[PropertyData]:
        props: List[PropertyData] = []
        try:
            with camoufox_context() as page:
                page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(8000)
                try:
                    fr = page.frame(url=lambda u: "powerbi" in (u or ""))
                except Exception as e:
                    logger.error("LOGS PowerBI frame missing: %s", e)
                    return []
                # The table visual hydrates slowly; wait for rows first.
                ready = False
                for _ in range(25):
                    try:
                        n = fr.evaluate(
                            "() => document.querySelectorAll("
                            "'[role=\"row\"]').length") or 0
                    except Exception as e:
                        logger.warning("LOGS row poll failed: %s", e)
                        n = 0
                    if n > 5:
                        ready = True
                        break
                    fr.wait_for_timeout(2000)
                if not ready:
                    logger.error("LOGS PowerBI table never hydrated")
                    return []
                rows = self._scroll_collect(fr)
        except Exception as e:
            logger.error("LOGS fetch failed: %s", e, exc_info=True)
            return []
        logger.info("LOGS: %d grid rows", len(rows))
        kept = skipped = 0
        for rec in rows:
            prop = self._parse_row(rec)
            if prop:
                props.append(prop)
                kept += 1
            else:
                skipped += 1
        logger.info("LOGS: %d kept, %d skipped", kept, skipped)
        return props

    # -- grid harvesting -------------------------------------------

    def _scroll_collect(self, fr) -> List[Dict[str, str]]:
        """Scroll the virtualized table until row count stabilizes."""
        seen: Dict[str, Dict[str, str]] = {}
        stable = 0
        for _ in range(MAX_SCROLLS):
            try:
                rows = fr.evaluate("""() => Array.from(
                  document.querySelectorAll('[role="row"]')).map(tr =>
                  Array.from(tr.querySelectorAll(
                    '[role="gridcell"],[role="cell"],td'))
                    .map(td => (td.innerText || '').trim()))""") or []
            except Exception:
                break
            before = len(seen)
            for cells in rows:
                if len(cells) < 7:
                    continue
                if cells[1].lower().startswith("county"):
                    continue  # header row
                key = "|".join(cells[1:7])
                seen[key] = {
                    "county": clean_html(cells[1]),
                    "sale": clean_html(cells[2]),
                    "time": clean_html(cells[3]),
                    "case": clean_html(cells[4]),
                    "address": clean_html(cells[5]),
                    "bid": clean_html(cells[6]),
                }
            try:
                fr.evaluate("""() => {
                  const sc = Array.from(document.querySelectorAll('*'))
                    .find(e => e.scrollHeight > e.clientHeight + 50 &&
                      (e.innerText || '').includes('Select Row'));
                  if (sc) sc.scrollTop = sc.scrollHeight; })""")
                fr.wait_for_timeout(1500)
            except Exception:
                break
            if len(seen) == before:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
        return list(seen.values())

    # -- row parsing -------------------------------------------------

    def _parse_row(self, rec: Dict[str, str]) -> Optional[PropertyData]:
        county_key = self.keep_county(rec.get("county"))
        if not county_key or not rec.get("case"):
            return None
        today = date.today().isoformat()
        sale_date = parse_sale_date(rec.get("sale"))
        if sale_date is not None and sale_date < today:
            return None
        street, city, zip_code = split_address(
            rec.get("address") or "", "North Carolina")
        time_raw = rec.get("time") or ""
        desc = (f"Sale: {rec.get('sale')} {time_raw} | Case: {rec.get('case')} | "
                f"{rec.get('address')} | Bid: {rec.get('bid')}")
        return self.build_property(
            listing_id=rec["case"],
            county=rec.get("county") or "",
            address=street,
            city=city,
            zip_code=zip_code,
            court_case=rec.get("case"),
            sale_date=sale_date,
            price_dollars=price_to_dollars(rec.get("bid")),
            description=f"[LOGS] {desc}",
            raw_text=desc,
        )


def scrape_all() -> List[PropertyData]:
    return LogsNCScraper().scrape()
