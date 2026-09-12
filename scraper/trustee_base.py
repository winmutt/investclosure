"""Shared base for law-firm trustee-sale (mortgage foreclosure) scrapers.

Covers rlselaw, logs_nc, bellcarrington, brockandscott and
foreclosuretennessee: server-rendered tables / search grids of upcoming
non-judicial (deed-of-trust) sales. Every row produced here is a
mortgage foreclosure for the dashboard Mtg tab
(``property_type="mortgage_foreclosure"``).

Reusable pieces: HTML cleaning, price/date/address parsing, target-county
filtering, HTML-table reading, and a mortgage-PropertyData builder with
state-appropriate map/GIS links.
"""
from __future__ import annotations
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .base import BaseScraper, PropertyData
from .gis_urls import get_ga_gis_url, get_tn_gis_url
from .nc_gis_lookup import (
    build_gis_url,
    build_google_maps_topo_url,
    build_google_maps_url,
)

logger = logging.getLogger(__name__)

_HTML_STRIP = re.compile(r"<[^>]+>")


def clean_html(text: Any) -> str:
    """Strip tags, collapse whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(_HTML_STRIP, " ", str(text))).strip()


def price_to_dollars(text: Any) -> Optional[float]:
    """'$228,616.00' -> 228616.0; '$0.00'/blank/unparseable -> None."""
    if not text:
        return None
    try:
        value = float(re.sub(r"[^\d.]", "", str(text)))
    except (ValueError, TypeError):
        return None
    return value if value > 0 else None


_MONTHS_FULL = ("January|February|March|April|May|June|July|August|September|"
                "October|November|December")
_MONTHS_ABBR = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
_DATE_RES = [
    # 10/06/2026, 07/29/2026 (trailing time/parenthetical stripped first)
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),
     lambda m: (int(m.group(3)), int(m.group(1)), int(m.group(2)))),
    # 09/17/26 (2-digit year -> 20xx)
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2})\b"),
     lambda m: (2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))),
    # October 7, 2026 / Oct 7, 2026 / Sep 14, 2026
    (re.compile(
        r"(" + _MONTHS_FULL + r"|" + _MONTHS_ABBR + r")\s+(\d{1,2}),?\s+(\d{4})",
        re.IGNORECASE),
     lambda m: (int(m.group(3)),
                datetime.strptime(m.group(1)[:3].title().replace("Sept", "Sep"),
                                  "%b").month, int(m.group(2)))),
    # 2026-09-01
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),
     lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
]


def parse_sale_date(text: Any) -> Optional[str]:
    """Parse a sale date in assorted firm formats -> ISO 'YYYY-MM-DD'."""
    if not text:
        return None
    core = re.split(r"\(|·|\b\d{1,2}:\d{2}", str(text), maxsplit=1)[0].strip()
    for rx, build in _DATE_RES:
        m = rx.search(core)
        if not m:
            continue
        try:
            y, mo, d = build(m)
            if y < 1000:
                continue  # typo'd year (e.g. sheet "0206") — never actionable
            return date(y, mo, d).isoformat()
        except (ValueError, IndexError):
            continue
    return None


_STREET_SUFFIX = (
    r"STREET|ST|AVENUE|AVE|BOULEVARD|BLVD|DRIVE|DR|ROAD|RD|LANE|LN|"
    r"HIGHWAY|HWY|COURT|CT|CIRCLE|CIR|PIKE|WAY|TRAIL|PLACE|PL|PARKWAY|PKWY"
)

_SUFFIX_ADDR_RE = re.compile(
    r"^(\d+\s+.*?(?:" + _STREET_SUFFIX + r")\.?)\s*,?\s+([A-Za-z][A-Za-z .'-]*?)\s*$",
    re.IGNORECASE,
)


def split_address(address: str, state_name: str = "") -> tuple[Optional[str], Optional[str], Optional[str]]:
    """'1306 Shellbark Ct Havelock, North Carolina 28532' ->
    ('1306 Shellbark Ct', 'Havelock', '28532'). Multi-word cities
    ('611 West 26 St Winston Salem ...') handled via street-suffix anchor.
    Falls back to (addr, None, None)."""
    if not address:
        return None, None, None
    text = clean_html(address)
    if state_name:
        m = re.match(r"^(.*?)\s*,?\s*" + re.escape(state_name) + r"\s+(\d{5})\s*$",
                     text, re.IGNORECASE)
        if m:
            head, zip_code = m.group(1).strip(), m.group(2)
            sm = _SUFFIX_ADDR_RE.match(head)
            if sm:
                return sm.group(1).strip(), sm.group(2).strip(), zip_code
            return head or None, None, zip_code
    # 'Street, City' fallback (Kania style).
    parts = [p.strip() for p in re.split(r",\s*", text) if p.strip()]
    if len(parts) >= 2:
        return ", ".join(parts[:-1]), parts[-1], None
    return text, None, None


def read_html_table(page, index: int = 0) -> List[List[str]]:
    """Return one rendered HTML table as rows of cell text."""
    try:
        return page.evaluate(
            """(idx) => {
              const tables = Array.from(document.querySelectorAll('table'));
              if (idx >= tables.length) return [];
              return Array.from(tables[idx].querySelectorAll('tr')).map(tr =>
                Array.from(tr.querySelectorAll('th, td'))
                  .map(c => (c.innerText || '').trim()));
            }""",
            index,
        ) or []
    except Exception as e:
        logger.warning("read_html_table failed: %s", e)
        return []


class TrusteeSaleScraper(BaseScraper):
    """Base for trustee-sale listing scrapers (all Mtg-tab rows)."""

    SOURCE_NAME = "trustee_sale"
    STATE = ""  # e.g. "GA"
    TARGET_COUNTIES: set[str] = set()  # lowercase county names
    LIST_URL = ""

    def __init__(self, delay_range: tuple[float, float] = (0.5, 1.5)):
        super().__init__(delay_range=delay_range, use_selenium=False)

    # -- shared row pipeline -------------------------------------------

    def keep_county(self, county: Any) -> Optional[str]:
        """Lowercased county if in the target set, else None."""
        name = (county or "").strip().lower()
        return name if name and name in self.TARGET_COUNTIES else None

    def build_property(
        self,
        *,
        listing_id: str,
        county: str,
        address: Optional[str] = None,
        city: Optional[str] = None,
        zip_code: Optional[str] = None,
        parcel_number: Optional[str] = None,
        court_case: Optional[str] = None,
        sale_date: Optional[str] = None,
        price_dollars: Optional[float] = None,
        acres: Optional[float] = None,
        description: Optional[str] = None,
        raw_text: Optional[str] = None,
        url: Optional[str] = None,
        state: Optional[str] = None,
    ) -> PropertyData:
        """Build a mortgage_foreclosure PropertyData with map/GIS links."""
        state = state or self.STATE
        county_t = (county or "").strip()
        gis_url: Optional[str] = None
        if state == "GA":
            gis_url = get_ga_gis_url(county_t, parcel_number or "")
        elif state == "TN":
            gis_url = get_tn_gis_url(county_t, parcel_number or "")
        elif state == "NC" and (parcel_number or address):
            gis_url = build_gis_url(None, None, parcel_number,
                                    address, county_t, state="NC")
        maps_url = (build_google_maps_url(None, None, address, city,
                                          county_t, state=state or None)
                    if address else None)
        topo_url = (build_google_maps_topo_url(None, None, address, city,
                                               county_t, state=state or None)
                    if address else None)
        prop: PropertyData = {
            "source": self.SOURCE_NAME,
            "source_listing_id": listing_id,
            "url": url or self.LIST_URL,
            "address": address,
            "city": city,
            "county": county_t or None,
            "state": state or None,
            "zip_code": zip_code,
            "latitude": None,
            "longitude": None,
            "price": price_dollars,
            "acres": acres,
            "description": description,
            "property_type": "mortgage_foreclosure",
            "image_url": None,
            "parcel_number": parcel_number,
            "court_case": court_case,
            "auction_date": sale_date,
            "gis_url": gis_url,
            "google_maps_url": maps_url,
            "google_maps_topo_url": topo_url,
            "raw_source_text": raw_text or description,
        }
        return prop
