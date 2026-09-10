"""ForeclosureTennessee.com trustee-sale scraper (mortgage foreclosures).

https://foreclosuretennessee.com is an ASP.NET public-notice platform
(Tennessee Bankers Association): a public results grid
(Home-Public.aspx — Sale Date | Continuance Date | City | Address | Zip |
County | Firm/Trustee | Listing Link, ~4 pages) plus per-notice detail
pages (Foreclosure-Listing.aspx?submissionID=N) with trustee, address and
OCR notice text. Filtered to the 37 TN mountain counties; every row is a
mortgage/deed-of-trust sale for the Mtg tab.
"""
from __future__ import annotations
import logging
import re
import time
from datetime import date
from typing import Dict, List, Optional
from urllib.parse import urljoin

from .base import PropertyData, camoufox_context
from .config import TN_FORECLOSURE_COUNTIES
from .trustee_base import (
    TrusteeSaleScraper, clean_html, parse_sale_date, read_html_table,
)

logger = logging.getLogger(__name__)

GRID_URL = ("https://foreclosuretennessee.com/Foreclosure/Foreclosure/"
            "Home-Public.aspx?hkey=0ad281c0-2fb4-42bd-9e64-89bfceea2fc7")
BASE_URL = "https://foreclosuretennessee.com"
MAX_PAGES = 6


class ForeclosureTennesseeScraper(TrusteeSaleScraper):
    SOURCE_NAME = "foreclosuretennessee"
    STATE = "TN"
    TARGET_COUNTIES = set(TN_FORECLOSURE_COUNTIES)
    LIST_URL = GRID_URL

    def scrape(self) -> List[PropertyData]:
        props: List[PropertyData] = []
        try:
            with camoufox_context() as page:
                page.goto(GRID_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
                seen_rows: set[str] = set()
                for _ in range(MAX_PAGES):
                    for rec in self._grid_rows(page):
                        key = rec.get("link") or (rec.get("address"), rec.get("county"))
                        if key in seen_rows:
                            continue
                        seen_rows.add(key)
                        prop = self._scrape_row(page, rec)
                        if prop:
                            props.append(prop)
                        time.sleep(1)
                    if not self._next_page(page, len(seen_rows)):
                        break
        except Exception as e:
            logger.error("ForeclosureTennessee fetch failed: %s", e, exc_info=True)
        logger.info("ForeclosureTennessee: %d kept", len(props))
        return props

    # -- grid ----------------------------------------------------

    def _grid_rows(self, page) -> List[Dict[str, str]]:
        try:
            links = page.evaluate("""() => Array.from(
                document.querySelectorAll('a[href*="Foreclosure-Listing.aspx"]'))
                .map(a => ({href: a.href, row: (a.closest('tr') || {})
                  && Array.from((a.closest('tr') || {children: []}).children || [])
                  .map(td => (td.innerText || '').trim())}))""") or []
        except Exception:
            return []
        recs = []
        for item in links:
            cells = item.get("row") or []
            # Grid columns: Sale Date | Continuance | City | Address | Zip |
            # County | Firm/Trustee | Listing Link
            recs.append({
                "link": item.get("href"),
                "sale": clean_html(cells[0]) if len(cells) > 0 else "",
                "cont": clean_html(cells[1]) if len(cells) > 1 else "",
                "city": clean_html(cells[2]) if len(cells) > 2 else "",
                "address": clean_html(cells[3]) if len(cells) > 3 else "",
                "zip": clean_html(cells[4]) if len(cells) > 4 else "",
                "county": clean_html(cells[5]) if len(cells) > 5 else "",
                "firm": clean_html(cells[6]) if len(cells) > 6 else "",
            })
        return recs

    def _next_page(self, page, before: int) -> bool:
        """Click the next pager number; True if the grid advanced."""
        try:
            advanced = page.evaluate("""() => {
              const els = Array.from(document.querySelectorAll('a'));
              const cur = document.querySelector('span[style*=\"font-weight\"]');
              const curN = cur ? parseInt((cur.innerText || '').trim()) : 1;
              for (const a of els) {
                const t = (a.innerText || '').trim();
                if (/^\\d+$/.test(t) && parseInt(t) === curN + 1) {
                  a.click(); return true;
                }
              }
              return false;
            }""")
            if not advanced:
                return False
            page.wait_for_timeout(6000)
            return True
        except Exception:
            return False

    # -- detail --------------------------------------------------

    _LABELS = {
        "Submission ID": "submission",
        "Sale Date": "sale_date",
        "Continuance Date": "cont_date",
        "Trustee Name": "trustee",
        "Property Address": "address",
        "City/State": "city_state",
        "Zipcode": "zip",
        "County": "county",
    }

    def _scrape_row(self, page, rec: Dict[str, str]) -> Optional[PropertyData]:
        if not self.keep_county(rec.get("county")) or not rec.get("link"):
            return None
        try:
            page.goto(rec["link"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            body = page.inner_text("body") or ""
        except Exception as e:
            logger.warning("FTN detail failed %s: %s", rec.get("link"), e)
            return None
        return self._parse_detail(body, rec)

    def _parse_detail(self, body: str, rec: Dict[str, str]) -> Optional[PropertyData]:
        """Parse detail body text + grid record -> PropertyData|None (pure)."""
        today = date.today().isoformat()
        fields: Dict[str, str] = {}
        for label, key in self._LABELS.items():
            m = re.search(label + r"\s*:\s*(.+)", body)
            if m:
                fields[key] = clean_html(m.group(1).split("\n")[0])[:160]
        sale_date = parse_sale_date(fields.get("cont_date") or fields.get("sale_date")
                                    or rec.get("cont") or rec.get("sale"))
        if sale_date is not None and sale_date < today:
            return None
        county = fields.get("county") or rec.get("county") or ""
        if not self.keep_county(county):
            return None
        address = fields.get("address") or rec.get("address") or None
        city = (fields.get("city_state") or "").split(",")[0].strip() or rec.get("city") or None
        zip_code = fields.get("zip") or rec.get("zip") or None
        trustee = fields.get("trustee") or rec.get("firm") or ""
        sub = fields.get("submission") or ""
        desc = (f"Submission: {sub} | Trustee: {trustee} | Sale: "
                f"{fields.get('sale_date', '')} | Continuance: {fields.get('cont_date', '')}")
        return self.build_property(
            listing_id=f"ftn:{sub}" if sub else f"ftn:{rec['link'].split('=')[-1]}",
            county=county,
            address=address,
            city=city,
            zip_code=zip_code,
            sale_date=sale_date,
            description=desc,
            raw_text=body[:4000],
            url=rec["link"],
        )


def scrape_all() -> List[PropertyData]:
    return ForeclosureTennesseeScraper().scrape()
