"""Haywood County (NC) tax foreclosure monitor.

The info page (https://www.haywoodcountync.gov/337/Tax-Foreclosures) carries
process text only. Live sales, when posted, appear in the county's CivicPlus
bid module:

    https://www.haywoodcountync.gov/Bids.aspx?CatID=17&txtSort=Category&showAllBids=&Status=open

Currently "no open bid postings" — so this scraper is a monitor: empty runs
log kept_empty and return []; the moment bid rows appear they are parsed
into per-posting rows (content-hashed ids) with zero code change.

No captcha, no Turnstile.
"""
from __future__ import annotations

import logging
import re

from .base import BaseScraper, PropertyData
from . import county_static as cs
from .rawlog import log_raw

logger = logging.getLogger(__name__)

INFO_URL = "https://www.haywoodcountync.gov/337/Tax-Foreclosures"
BIDS_URL = ("https://www.haywoodcountync.gov/Bids.aspx?CatID=17"
            "&txtSort=Category&showAllBids=&Status=open")
COUNTY = "Haywood"


class HaywoodCountyScraper(BaseScraper):
    """Monitor Haywood County's CivicPlus bid postings for tax foreclosure sales."""

    SOURCE_NAME = "haywood_county"
    BASE_URL = BIDS_URL

    def __init__(self, delay_range: tuple[float, float] = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)

    def scrape(self) -> list[PropertyData]:
        logger.info("Checking Haywood County bid postings ...")
        try:
            page = cs.fetch_page(BIDS_URL)
        except Exception as e:
            logger.error("Haywood County fetch failed: %s", e)
            return []
        body = page.get("body") or ""
        if re.search(r"no open bid postings|no .*bids? (available|at this time)", body, re.I):
            cs.log_kept_empty(
                self.SOURCE_NAME, COUNTY, "no open bid postings",
                body[:2000], BIDS_URL,
            )
            logger.info("Haywood County: no open postings")
            return []

        try:
            links = cs.find_links(BIDS_URL, pattern=r"BidDetail|bids\.aspx\?.*BidID|foreclosure|sale")
        except Exception:
            links = []
        postings = [l for l in links if l.get("text") and "skip to" not in l["text"].lower()]
        properties: list[PropertyData] = []
        for l in postings:
            text = l["text"]
            digest = cs.content_hash(text + l["href"])
            lid = f"haywood_{digest}"
            price = cs.parse_money(text)
            sale_iso = cs.to_iso_date(text)
            case = cs.find_case(text)
            log_raw(
                self.SOURCE_NAME, listing_id=lid, county=COUNTY, state="NC",
                decision="kept_tax", reason="bid posting row",
                raw_text=f"{text} | {l['href']}", url=BIDS_URL,
            )
            properties.append(cs.build_tax_row(
                source=self.SOURCE_NAME, listing_id=lid, url=l["href"],
                county=COUNTY, price=price, auction_date=sale_iso,
                court_case=case,
                description=f"[Haywood Co] {text[:300]}",
                raw_text=f"{text} | {l['href']}", extra_key="bid",
            ))
        if not properties:
            cs.log_kept_empty(
                self.SOURCE_NAME, COUNTY, "page changed but no postings parsed",
                body[:2000], BIDS_URL,
            )
        else:
            logger.info("Haywood County: %d postings", len(properties))
        return properties
