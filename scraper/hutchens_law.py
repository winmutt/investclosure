"""Hutchens Law Firm - NC Foreclosure Sales List scraper.

Scrapes the Hutchens Law Firm NC foreclosure sales listing page hosted on
sales.hutchenslawfirm.com which uses Telerik RadGrid with a static HTML table.

Data from each row:
  Case No., SP#, County, Sale Date, Property Address, Property CSZ,
  Deed of Trust Book/Page, Bid Amount

The page uses ASP.NET postbacks for filtering (text search), but the base
list page returns all records in a single static table load.
"""
from __future__ import annotations
import logging
import re
from typing import Optional, Any

from .base import BaseScraper, PropertyData
from .config import QUALIFYING_COUNTIES

logger = logging.getLogger(__name__)


HUTCHENS_URL = "https://sales.hutchenslawfirm.com/NCfcSalesList.aspx"


class HutchensLawScraper(BaseScraper):
    SOURCE_NAME = "hutchens_law"
    MIN_ACRES = 5.0

    def __init__(self, delay_range: tuple[float, float] = (0.5, 1.5)):
        super().__init__(delay_range=delay_range, use_selenium=False)

    def scrape(self) -> list[PropertyData]:
        """Fetch all NC foreclosure sales records from the Hutchens Law Firm listing page."""
        NC_QUALIFYING = {c.lower() for c in QUALIFYING_COUNTIES.get("NC", [])}

        session = self.session
        session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
        })

        logger.info("Fetching Hutchens Law NC foreclosure sales list ...")

        try:
            resp = session.get(HUTCHENS_URL, timeout=30)
            if resp.status_code != 200:
                logger.error("Hutchens Law returned HTTP %d", resp.status_code)
                return []

            html = resp.text
            logger.info("Page size: %d bytes", len(html))

            properties = self._parse_table(html, NC_QUALIFYING)
            logger.info("Got %d qualifying properties from Hutchens Law", len(properties))
            return properties

        except Exception as e:
            logger.error("Hutchens Law fetch failed: %s", e, exc_info=True)
            return []

    # ------------------------------------------------------------------
    _ROW_REGEX = re.compile(
        r'<tr class="(?:GridRow_WebBlue|GridAltRow_WebBlue)">\s*'
        r'<td[^>]*>(.*?)</td>\s*'
        r'<td[^>]*>(.*?)</td>\s*'
        r'<td[^>]*>(.*?)</td>\s*'
        r'<td[^>]*>(.*?)</td>\s*'
        r'<td[^>]*>(.*?)</td>\s*'
        r'<td[^>]*>(.*?)</td>\s*'
        r'<td[^>]*>(.*?)</td>\s*'
        r'<td[^>]*>(.*?)</td>\s*'
        r'</tr>',
        re.DOTALL,
    )

    _NON_WHITESPACE = re.compile(r'^\S+$')

    @staticmethod
    def _parse_bid(text: str) -> Optional[float]:
        """Parse bid amount text into a float (dollars).
        
        Examples:
            "$169,894.26" -> 169894.26
            "Bid not available yet" -> None
            "Bid upset 07/24/2026, increasing bid to $127,248.07" -> 127248.07
        """
        text = text.strip()
        if not text or "bid" in text.lower() and "not available" in text.lower():
            # Check for "Bid not available yet" or similar
            if "not available" in text.lower() or "bid not" in text.lower():
                return None
        
        # Try to find a dollar amount with cents
        dollar_pattern = r'\$((\d{1,3}(?:,\d{3})*(?:\.\d{2}?)))'
        matches = re.findall(dollar_pattern, text)
        if matches:
            # Take the last (largest/most recent) bid if there are upset bid changes
            last_bid = matches[-1][0].replace(",", "")
            try:
                return float(last_bid)
            except ValueError:
                pass
        
        return None

    @staticmethod
    def _parse_cszip(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Parse CSZ field into (city, state, zip).
        
        Examples:
            "Leicester, NC 28748" -> ("Leicester", "NC", "28748")
            "Clemmons, NC 27012-7296" -> ("Clemmons, NC 27012", "NC", "27012")
            "Asheville, NC 28806" -> ("Asheville", "NC", "28806")
        """
        text = text.strip()
        # Pattern: "City, ST ZIP" or "City, ST ZIP-EXT"
        m = re.search(r'^(.+?),\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$', text)
        if m:
            return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        return text, None, None

    @staticmethod
    def _clean(text: str) -> str:
        """Strip HTML tags and whitespace."""
        text = re.sub(r'<[^>]+>', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _parse_saledate(text: str) -> Optional[str]:
        """Parse sale date 'M/D/YYYY' into 'YYYY-MM-DD', or return None."""
        text = text.strip()
        if not text or "not" in text.lower():
            return None
        # Try M/D/YYYY format
        m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', text)
        if m:
            month, day, year = m.group(1), m.group(2), m.group(3)
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        return None

    def _parse_table(self, html: str, county_filter: set[str]) -> list[PropertyData]:
        """Parse the RadGrid HTML table into PropertyData list."""
        matches = self._ROW_REGEX.findall(html)
        
        if not matches:
            logger.warning("No table rows found in Hutchens Law page")
            return []

        logger.info("Found %d table rows in Hutchens page", len(matches))

        properties: list[PropertyData] = []
        skipped = 0

        for case_no_raw, sp_no_raw, county_raw, saledate_raw, \
                address_raw, csz_raw, deed_raw, bid_raw in matches:

            # Clean values
            case_no = self._clean(case_no_raw)
            sp_no = self._clean(sp_no_raw)
            county_raw_str = self._clean(county_raw)
            saledate_raw_str = self._clean(saledate_raw)
            address = self._clean(address_raw)
            deed = self._clean(deed_raw)
            
            # Parse county - extract just the county name
            county_match = re.match(r'^(.+?),\s*NC$', county_raw_str)
            county = county_match.group(1).strip().lower() if county_match else ""
            state = "NC"

            # Filter by qualifying NC mountain counties
            if not county or county not in county_filter:
                skipped += 1
                continue

            # Parse CSZ
            city, st, zip_code = self._parse_cszip(self._clean(csz_raw))
            if city:
                city = self._clean(city)
            if st:
                state = st
            # If city didn't parse, clean the original CSZ to get city
            if not city:
                city = self._clean(csz_raw)

            # Parse bid
            bid = self._parse_bid(self._clean(bid_raw))

            # Parse sale date
            sale_date = self._parse_saledate(self._clean(saledate_raw))

            prop: PropertyData = {
                "source": self.SOURCE_NAME,
                "source_listing_id": case_no or sp_no or None,
                "court_case": sp_no or None,
                "url": f"{HUTCHENS_URL}",
                "address": address if address else None,
                "city": city if city else None,
                "county": county if county else None,
                "state": state,
                "zip_code": zip_code if zip_code else None,
                "latitude": None,
                "longitude": None,
                "price": bid,
                "acres": None,
                "acres_source": "placeholder",
                "description": self._build_description(
                    saledate_raw_str, deed, bid, sp_no, case_no
                ),
                "property_type": "Foreclosure Sale",
                "image_url": None,
                "parcel_number": None,
                "deed_book": deed if deed and not re.search(r'not|unavailable', deed, re.IGNORECASE) else None,
                "gis_url": None,
                "auction_date": sale_date,
                "close_date": None,
                "upset_bid": None,
                "foreclosure_key": "|".join([case_no, saledate_raw_str, bid_raw, deed]),
                "google_maps_url": None,
                "google_maps_topo_url": None,
                "sale_date": sale_date,
            }

            properties.append(prop)

        if skipped:
            logger.info("Hutchens: skipped %d rows from non-qualifying counties", skipped)

        return properties

    @staticmethod
    def _build_description(
        saledate: str, deed: str, bid_amount: Optional[float], sp_no: str, case_no: str
    ) -> Optional[str]:
        """Build a human-readable description string."""
        parts = []
        if saledate:
            parts.append(f"Sale: {saledate}")
        if bid_amount is not None:
            parts.append(f"Bid: ${bid_amount:,.2f}")
        if sp_no and not re.search(r'not|unavailable', sp_no, re.IGNORECASE):
            parts.append(f"SP#: {sp_no}")
        if case_no:
            parts.append(f"Case: {case_no}")
        if deed and not re.search(r'not|unavailable', deed, re.IGNORECASE):
            parts.append(f"Deed: {deed}")
        return " | ".join(parts) if parts else None
