"""Bell Carrington trustee-sale scraper (mortgage foreclosures).

https://bellcarrington.com/foreclosure-sales/ embeds a published Google
Sheet with all active sales (GA/SC/NC/AL/TN). Primary path downloads the
sheet's CSV export through the camoufox session; fallback scrapes the
rendered pubhtml grid. Filtered to our mountain-county sets per state;
every row is a mortgage/deed-of-trust sale for the Mtg tab.
"""
from __future__ import annotations
import csv
import io
import logging
import re
import time
from datetime import date
from typing import Dict, List, Optional

from .base import PropertyData, camoufox_context, CamoufoxFetcher
from .config import (
    GA_MOUNTAIN_COUNTIES,
    NC_MOUNTAIN_COUNTIES,
    TN_FORECLOSURE_COUNTIES,
    QUALIFYING_COUNTIES,
)
from .trustee_base import (
    TrusteeSaleScraper, clean_html, parse_sale_date, price_to_dollars,
)

logger = logging.getLogger(__name__)

LIST_URL = "https://bellcarrington.com/foreclosure-sales/"

STATE_COUNTIES: Dict[str, set[str]] = {
    "GA": set(GA_MOUNTAIN_COUNTIES),
    "NC": set(NC_MOUNTAIN_COUNTIES),
    "TN": set(TN_FORECLOSURE_COUNTIES),
    "SC": set(QUALIFYING_COUNTIES.get("SC", [])),
    "AL": set(QUALIFYING_COUNTIES.get("AL", [])),
}
STATE_ALIASES = {
    "georgia": "GA", "ga": "GA",
    "south carolina": "SC", "sc": "SC",
    "north carolina": "NC", "nc": "NC",
    "alabama": "AL", "al": "AL",
    "tennessee": "TN", "tn": "TN",
}


class BellCarringtonScraper(TrusteeSaleScraper):
    SOURCE_NAME = "bellcarrington"
    STATE = ""  # multi-state; set per row
    TARGET_COUNTIES = set()
    LIST_URL = LIST_URL

    def scrape(self) -> List[PropertyData]:
        try:
            with camoufox_context() as page:
                sheet_id = self._sheet_id(page)
                rows = self._fetch_csv(page, sheet_id) if sheet_id else []
                if not rows:
                    rows = self._fetch_grid(page)
        except Exception as e:
            logger.error("BellCarrington fetch failed: %s", e, exc_info=True)
            return []
        logger.info("BellCarrington: %d sheet rows", len(rows))
        return self._parse_rows(rows)

    # -- fetch -----------------------------------------------------

    def _sheet_id(self, page) -> Optional[str]:
        try:
            page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            srcs = page.evaluate(
                "() => Array.from(document.querySelectorAll('iframe'))"
                ".map(i => i.src || '')")
        except Exception:
            return None
        for src in srcs or []:
            m = re.search(r"/spreadsheets/d/e/([A-Za-z0-9_-]+)/", src)
            if m:
                return m.group(1)
        return None

    def _fetch_csv(self, page, sheet_id: str) -> List[Dict[str, str]]:
        url = (f"https://docs.google.com/spreadsheets/d/e/{sheet_id}/"
               f"pub?gid=0&single=true&output=csv")
        try:
            fetcher = CamoufoxFetcher(page)
            data = fetcher.get(url, timeout=60000, download=True)
        except Exception as e:
            logger.warning("BellCarrington CSV failed: %s", e)
            return []
        if "<html" in data[:500].lower():
            logger.warning("BellCarrington CSV returned HTML (consent wall)")
            return []
        try:
            lines = data.splitlines()
            head = next(
                (i for i, ln in enumerate(lines) if "county" in ln.lower()),
                None)
            if head is None:
                logger.warning("BellCarrington CSV has no header row")
                return []
            reader = csv.DictReader(lines[head:])
            # Normalize header whitespace (' Sale Date' -> 'sale date').
            out = []
            for row in reader:
                out.append({(k or "").strip().lower(): (v or "")
                            for k, v in row.items()})
            return out
        except Exception as e:
            logger.warning("BellCarrington CSV parse failed: %s", e)
            return []

    def _fetch_grid(self, page) -> List[Dict[str, str]]:
        """Fallback: read the rendered pubhtml sheet grid into dict rows."""
        try:
            rows = page.evaluate("""() => {
              const frames = Array.from(document.querySelectorAll('iframe'));
              const docs = [document];
              // same-origin access only; cross-origin frames are skipped
              return Array.from(document.querySelectorAll('table.waffle tr'))
                .map(tr => Array.from(tr.querySelectorAll('td'))
                .map(td => (td.innerText || '').trim()));
            }""") or []
        except Exception as e:
            logger.warning("BellCarrington grid fallback failed: %s", e)
            return []
        if len(rows) < 2:
            return []
        heads = [h.lower() for h in rows[0]]
        return [dict(zip(heads, r)) for r in rows[1:] if any(r)]

    # -- parse -----------------------------------------------------

    @staticmethod
    def _col(row: Dict[str, str], *names: str) -> str:
        keys = {k.lower().strip(): k for k in row.keys()}
        for name in names:
            if name in keys:
                return clean_html(row[keys[name]])
        return ""

    def _parse_rows(self, rows: List[Dict[str, str]]) -> List[PropertyData]:
        today = date.today().isoformat()
        props: List[PropertyData] = []
        kept = skipped = bad_date = 0
        section_state = ""
        for row in rows:
            cells = [clean_html(v) for v in row.values()]
            non_empty = [c for c in cells if c]
            # Section title rows (`,GA,,,,,,`) set the state for rows below.
            if len(non_empty) == 1 and non_empty[0].lower() in STATE_ALIASES:
                section_state = STATE_ALIASES[non_empty[0].lower()]
                continue
            # Repeated header rows.
            if any(c.lower() == "sale date" for c in cells):
                continue
            state = STATE_ALIASES.get(
                self._col(row, "state", "st").lower(), "") or section_state
            county = self._col(row, "county", "co.")
            if not state or state not in STATE_COUNTIES:
                skipped += 1
                continue
            if county.strip().lower() not in STATE_COUNTIES[state]:
                skipped += 1
                continue
            sale_raw = self._col(row, "sale date", "saledate", "sale",
                                 "auction date")
            sale_date = parse_sale_date(sale_raw)
            if sale_date is None and sale_raw:
                bad_date += 1  # unparseable/typo'd year — never actionable
                skipped += 1
                continue
            if sale_date is not None and sale_date < today:
                skipped += 1
                continue
            address = (self._col(row, "property address", "property",
                                 "address", "street address", "location")
                       or None)
            parcel = (self._col(row, "parcel", "parcel number",
                                "parcel #", "map parcel") or None)
            case_no = (self._col(row, "case number", "case #", "case no",
                                 "file #", "loan #") or None)
            if not address and not parcel:
                skipped += 1
                continue
            listing_id = (parcel or case_no or
                          f"{county}-{address}" if (parcel or case_no or address)
                          else None)
            if not listing_id:
                skipped += 1
                continue
            desc = " | ".join(f"{k}: {clean_html(v)}" for k, v in row.items()
                              if clean_html(v))[:2000]
            props.append(self.build_property(
                listing_id=f"bell:{state.lower()}:{listing_id}",
                county=county,
                state=state,
                address=address,
                city=(self._col(row, "city") or None),
                zip_code=(self._col(row, "zip", "zip code") or None),
                parcel_number=parcel,
                court_case=case_no,
                sale_date=sale_date,
                price_dollars=price_to_dollars(
                    self._col(row, "opening bid", "bid", "bid amount")),
                description=f"[Bell Carrington] {desc}",
                raw_text=desc,
            ))
            kept += 1
        logger.info("BellCarrington: %d kept, %d skipped (%d bad dates)",
                    kept, skipped, bad_date)
        return props


def scrape_all() -> List[PropertyData]:
    return BellCarringtonScraper().scrape()
