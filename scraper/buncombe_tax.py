"""Buncombe County (NC) tax foreclosure scraper.

Buncombe County publishes its tax foreclosure auction listings on a Trumba
calendar at taxforeclosures.buncombenc.gov. The calendar exposes a structured
iCal feed that contains everything we need:

    SUMMARY                         owner name (HELEN LUNSFORD)
    LOCATION                        street address (55 Whittemore Branch Rd)
    DTSTART;TZID=America/New_York   bidding begins (20260807T120000)
    DTEND;TZID=America/New_York     bidding ends   (20260817T170000)
    X-TRUMBA-CUSTOMFIELD "Opening/Current Bid"   current upset bid (13,851.33)
    X-TRUMBA-CUSTOMFIELD "Redeemed"              "No" when still for sale
    X-TRUMBA-CUSTOMFIELD "Case Number"           court case (22 CVD 3028)
    X-TRUMBA-CUSTOMFIELD "PIN lookup"            parcel PIN (PINN=977539234200000)
    X-TRUMBA-CUSTOMFIELD "Property Type"         Land / Land & Structures
    X-TRUMBA-CUSTOMFIELD "Fire District"         fire district name
    DESCRIPTION                     HTML summary of the above fields
    X-TRUMBA-LINK                   detail page URL

The feed is fetched with a single HTTP GET (no captcha, no browser), parsed
without third-party libraries, and normalized into PropertyData. Buncombe is
one of the 21 NC mountain counties, so no county filtering is needed.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import requests

from .base import BaseScraper, PropertyData
from .config import config

logger = logging.getLogger(__name__)

ICAL_URL = "https://www.trumba.com/calendars/tax-foreclosures-all.ics"


def _unfold_ical(text: str) -> str:
    """Unfold iCalendar line continuations (RFC 5545)."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _unescape(value: str) -> str:
    """Decode iCalendar escaped characters."""
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_dt(value: str) -> Optional[str]:
    """Parse an iCal datetime into an ISO-ish 'YYYY-MM-DD HH:MM' string.

    Handles both local (TZID=America/New_York:20260807T120000) and UTC
    (20260807T120000Z) forms. Returns None if unparseable.
    """
    m = re.search(r"(\d{8})T(\d{6})(Z)?", value or "")
    if not m:
        return None
    date_part = m.group(1)
    time_part = m.group(2)
    tz_suffix = m.group(3) or ""
    return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[:2]}:{time_part[2:4]}" + tz_suffix


def _parse_price(value: str) -> Optional[float]:
    """Parse a currency string like '13,851.33' into a float."""
    cleaned = re.sub(r"[^\d.]", "", value or "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_pin_from_link(link: str) -> Optional[str]:
    """Extract the parcel PIN from a Trumba PIN-lookup URL."""
    if not link:
        return None
    m = re.search(r"PINN=([\d-]+)", link)
    if m:
        return m.group(1).strip()
    m = re.search(r"pid=([\d-]+)", link)
    if m:
        return m.group(1).strip()
    return None


def _custom_field(block: str, name: str) -> Optional[str]:
    """Return the value of an X-TRUMBA-CUSTOMFIELD by display NAME.

    Handles the two value encodings Trumba emits in its iCal:
      1. NAME="Opening/Current Bid";ID=58700;TYPE=Currency:13851.33
      2. NAME="Opening/Current Bid";...:<no value>  followed by
         X-TRUMBA-CUSTOMFIELD;FIELDVALUE;ID=58700 -> 13851.33
    """
    m = re.search(
        r'^X-TRUMBA-CUSTOMFIELD;NAME="' + re.escape(name) + r'";ID=(\d+);[^:\r\n]*?:(.*)$',
        block,
        re.MULTILINE,
    )
    if not m:
        return None
    field_id = m.group(1)
    value = m.group(2).strip()
    if not value or value.lower() in ("value", "n/a"):
        # Look up the FIELDVALUE continuation line for the same field id.
        m2 = re.search(
            r"^X-TRUMBA-CUSTOMFIELD;FIELDVALUE;ID=" + re.escape(field_id) + r"\s*->\s*(.*)$",
            block,
            re.MULTILINE,
        )
        if m2:
            return _unescape(m2.group(1).strip())
        return None
    return _unescape(value)


class BuncombeTaxScraper(BaseScraper):
    """Scraper for Buncombe County tax foreclosure listings via iCal feed."""

    SOURCE_NAME = "buncombe_tax"
    BASE_URL = "https://taxforeclosures.buncombenc.gov/"
    MIN_ACREAGE = config.MIN_ACRES

    def __init__(self, delay_range: tuple[float, float] = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)

    def scrape(self) -> list[PropertyData]:
        logger.info("Fetching Buncombe tax foreclosure iCal feed ...")
        try:
            resp = requests.get(
                ICAL_URL,
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"},
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error("Buncombe iCal fetch failed: %s", e)
            return []

        text = _unfold_ical(resp.text)
        blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.DOTALL)
        if not blocks:
            logger.warning("No VEVENT blocks found in Buncombe feed")
            return []

        properties: list[PropertyData] = []
        for block in blocks:
            prop = self._parse_event(block)
            if prop:
                properties.append(prop)
            self._random_delay()

        logger.info("Buncombe: %d events parsed", len(properties))
        return properties

    def _parse_event(self, block: str) -> Optional[PropertyData]:
        try:
            summary = _unescape(_field(block, "SUMMARY"))
            location = _unescape(_field(block, "LOCATION"))
            dtstart = _parse_dt(_field(block, "DTSTART"))
            dtend = _parse_dt(_field(block, "DTEND"))

            event_type = _custom_field(block, "Event Type")
            redeemed = _custom_field(block, "Redeemed")
            case_number = _custom_field(block, "Case Number")
            pin_link = _custom_field(block, "PIN lookup")
            prop_type = _custom_field(block, "Property Type")
            fire_district = _custom_field(block, "Fire District")
            bid_text = _custom_field(block, "Opening/Current Bid")
            description_raw = _unescape(_field(block, "DESCRIPTION"))

            # Skip redeemed events (owner paid, no longer for sale).
            if redeemed and "no" not in redeemed.lower():
                logger.info("Skipping redeemed event: %s", summary)
                return None

            # Skip non-foreclosure events if the feed ever carries them.
            if event_type and "foreclosure" not in event_type.lower():
                logger.info("Skipping non-foreclosure event: %s", summary)
                return None

            pin = _extract_pin_from_link(pin_link)
            price = _parse_price(bid_text)
            url = _field(block, "X-TRUMBA-LINK") or self.BASE_URL

            desc_parts = [p for p in [
                f"Opening/Current Bid: {bid_text}" if bid_text else None,
                f"Redeemed: {redeemed}" if redeemed else None,
                f"Case Number: {case_number}" if case_number else None,
                f"PIN: {pin}" if pin else None,
                f"Property Type: {prop_type}" if prop_type else None,
                f"Fire District: {fire_district}" if fire_district else None,
                f"Bidding Begins: {dtstart}" if dtstart else None,
                f"Bidding Ends: {dtend}" if dtend else None,
            ] if p]
            description = description_raw or (" | ".join(desc_parts) if desc_parts else None)

            prop: PropertyData = {
                "source": self.SOURCE_NAME,
                "source_listing_id": _extract_uid(_field(block, "UID")),
                "url": url,
                "address": location or None,
                "city": None,
                "county": "Buncombe",
                "state": "NC",
                "zip_code": None,
                "latitude": None,
                "longitude": None,
                "price": price,
                "acres": None,
                "acres_source": "placeholder",
                "description": description,
                "property_type": "tax_foreclosure",
                "image_url": None,
                "parcel_number": pin,
                "gis_url": self._get_gis_url(pin),
                "auction_date": dtstart or None,
                "close_date": None,
                "upset_bid": bid_text or None,
                "foreclosure_key": "|".join([case_number or "", pin or "", bid_text or ""]),
                "court_case": case_number or None,
                "initial_auction_date": dtstart or None,
                "upset_bid_end": dtend or None,
                "google_maps_url": self._gm(location, "Buncombe"),
                "google_maps_topo_url": self._gmt(location, "Buncombe"),
                "raw_source_text": description_raw or None,
            }
            return prop
        except Exception as e:
            logger.warning("Buncombe event parse error: %s", e)
            return None

    @staticmethod
    def _get_gis_url(pin: Optional[str]) -> Optional[str]:
        """Build the Buncombe County GIS parcel viewer URL from a PIN."""
        if not pin:
            return None
        return f"https://gis.buncombenc.gov/buncomap/Default.aspx?PINN={pin}"

    @staticmethod
    def _gm(address: Optional[str], county: str) -> Optional[str]:
        """Build a Google Maps search URL for the property."""
        parts = []
        if address:
            parts.append(address)
        if county:
            parts.append(county)
        parts.append("NC")
        return f"https://www.google.com/maps/search/{'+'.join(p for p in parts if p)}"

    @staticmethod
    def _gmt(address: Optional[str], county: str) -> Optional[str]:
        """Build a Google Maps terrain/topo URL for the property."""
        url = BuncombeTaxScraper._gm(address, county)
        if not url:
            return None
        return f"{url}/@?api=1&map_action=map&base=maps.terrain"


def _field(block: str, name: str) -> Optional[str]:
    """Return the raw value of a simple (non-custom-field) iCal property."""
    m = re.search(rf"^{re.escape(name)}(?:;[^:\r\n]*)?:(.*)$", block, re.MULTILINE)
    return m.group(1).strip() if m else None


def _extract_uid(uid: str) -> Optional[str]:
    """Return the numeric Trumba event id from the UID value."""
    if not uid:
        return None
    m = re.search(r"event/(\d+)", uid)
    if m:
        return f"trumba_{m.group(1)}"
    return uid.strip()
