"""GovEase online tax-sale auction schedule scraper.

GovEase (govease.com) is the auction platform many TN counties run their
Chancery Court Clerk & Master delinquent-tax sales through. The county
sites (Blount, Washington, Sullivan ...) publish no per-parcel lists on
the platform level without bidder login — all that is PUBLIC is the
master auction schedule, embedded on govease.com/auctions as a
Third-party "Awesome Table" (view-awesome-table.com) widget:

    County/City | State | Registration Date | Pre-Bidding Date |
    Pre-Bidding Time (CT) | Sale Date | Sale Time (CT) | Sale Type |
    Parcel List (link) | City

The widget renders every row directly into the rendered DOM (verified
live: 44 auctions nationwide). The scraper loads the widget URL directly,
reads the rendered rows via page.evaluate, and keeps the auctions whose
state is TN and whose county is one of our 38 targeted TN counties.
Each auction becomes a single ``tax_foreclosure`` listing row — the sale
date + sale type + parcel-list link. Per-parcel rows would need a
bidder-gated GovEase login, so the county's own channels (tnpublicnotice,
this scraper's schedule) cover discovery.

REAP: the schedule is a complete live+upcoming inventory — when an
auction vanishes from the widget (sale held/processed), the row is
archived as realized via ``reap_missing_listings`` (registered source).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .base import BaseScraper, PropertyData, camoufox_context
from .config import config

logger = logging.getLogger(__name__)

ACTIONS_URL = "https://www.govease.com/auctions"
AWT_URL = "https://view-awesome-table.com/-MF6O6OFpMxeDFAjyNJg/view"

# Order of rendered cells in every data row (verified 2026-10-22).
AWT_COLUMNS = (
    "county", "state", "registration_date", "prebid_date", "prebid_time",
    "sale_date", "sale_time", "sale_type", "parcel_list", "city",
)

_ROW_RE = re.compile(
    r"https://liveauctions\.govease\.com/[a-z]{2}/(?P<slug>[A-Za-z0-9]+)/(?P<auction_id>\d+)")

# Sale types that are NOT delinquent-property offerings. GovEase also runs
# county surplus/excess-proceeds sales ("Surplus Sale") which sell no land.
NON_TAX_SALE_TYPES = {"surplus sale"}


def _mdy_to_iso(value: Optional[str]) -> Optional[str]:
    """'11/10/2026' or '11/03/2026 08:00:00 AM' -> '2026-11-10'."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", value or "")
    if not m:
        return None
    mo, d, y = m.groups()
    try:
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        return None


class GovEaseScraper(BaseScraper):
    """Scraper for the GovEase live+upcoming tax-sale auction schedule."""

    SOURCE_NAME = "govease"
    BASE_URL = ACTIONS_URL
    MIN_ACREAGE = config.MIN_ACRES
    # live+upcoming schedule rows seen this run, including auctions later
    # dropped by the county filter — REAP uses this set (see run.py).
    # None = fetch failure (no signal); empty set = widget rendered but had
    # zero target TN rows.
    observed_listing_ids: Optional[set]

    def __init__(self, delay_range: tuple = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)
        self.observed_listing_ids = None

    # ---- scraping API ------------------------------------------------------

    def scrape(self) -> list[PropertyData]:
        """Load the Awesome-Table widget and read the rendered rows."""
        logger.info("Fetching GovEase auction schedule via Awesome Table ...")
        try:
            with camoufox_context() as page:
                page.goto(AWT_URL, timeout=60000, wait_until="domcontentloaded")
                # The widget renders rows after fetching the backing data.
                page.wait_for_timeout(6000)
                rows = self._read_rows(page)
        except Exception as e:
            logger.error("GovEase schedule fetch failed: %s", e)
            self.observed_listing_ids = None  # no signal on outage
            return []
        if len(rows) < 5:
            # Widget failed to populate (Google Sheets hiccup upstream) —
            # not a reliable "no auctions" signal.
            logger.warning("GovEase schedule rendered %d rows — treating as "
                           "widget failure, no REAP signal", len(rows))
            self.observed_listing_ids = None
            return []

        properties: list[PropertyData] = []
        for cells in rows:
            prop = self._parse_row(cells)
            if prop:
                properties.append(prop)
            self._random_delay()
        self._finalize_observed(rows)

        logger.info("GovEase: %d schedule rows, %d in target TN counties",
                    len(rows), len(properties))
        return properties

    # ---- parsing ----------------------------------------------------------

    @staticmethod
    def _read_rows(page) -> list[list[dict]]:
        rows = page.evaluate(
            """() => Array.from(document.querySelectorAll('tr'))
                .map(tr => Array.from(tr.cells).map(c => ({
                    t: c.innerText.trim(),
                    hrefs: Array.from(c.querySelectorAll('a')).map(a => a.href),
                })))
                .filter(cells => cells.length >= 9)""")
        return rows or []

    def _parse_row(self, cells: list[dict]) -> Optional[PropertyData]:
        county_cell = cells[0].get("t", "")
        state = cells[1].get("t", "").strip().upper()
        sale_type_raw = cells[7].get("t", "").strip()
        hrefs = cells[8].get("hrefs") or []
        city_raw = cells[9].get("t", "") if len(cells) > 9 else ""

        if not county_cell or state != "TN":
            return None
        if sale_type_raw.lower() in NON_TAX_SALE_TYPES:
            logger.info("Skipping GovEase %s (%s) — surplus sale", county_cell, sale_type_raw)
            return None

        county = _normalize_county(county_cell)
        if county is None:
            return None  # outside the 38 target counties
        slug, auction_id = _parse_liveauctions_url(hrefs[0] if hrefs else "",
                                                   county_cell)
        listing_id = f"{slug}-{auction_id}-{_slug_words(sale_type_raw)}"

        sale_date_port = _mdy_to_iso(cells[5].get("t", ""))
        prebid = _mdy_to_iso(cells[3].get("t", ""))
        reg = _mdy_to_iso(cells[2].get("t", ""))

        desc = f"GovEase online tax sale — {sale_type_raw}"
        if prebid:
            desc += f"; Pre-Bidding {prebid}"
        if reg:
            desc += f"; Registration {reg}"
        if hrefs and hrefs[0]:
            desc += f". Parcel list: {hrefs[0]}"
        raw_line = " | ".join(
            c.get("t", "").replace("\n", " / ")
            for c in (cells + [{}, {}, {}])[:10])

        prop: PropertyData = {
            "source": self.SOURCE_NAME,
            "source_listing_id": listing_id,
            "url": hrefs[0] if hrefs else self.BASE_URL,
            "address": None,
            "city": _clean_city(city_raw),
            "county": county,
            "state": "TN",
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": None,
            "acres": None,
            "description": desc,
            "property_type": "tax_foreclosure",
            "image_url": None,
            "parcel_number": None,
            "gis_url": None,
            "auction_date": sale_date_port,
            "close_date": None,
            "upset_bid": None,
            "foreclosure_key": listing_id,
            "court_case": None,
            "initial_auction_date": sale_date_port,
            "upset_bid_end": None,
            # address-less row → link-depth policy: no GIS/Maps search link
            "google_maps_url": None,
            "google_maps_topo_url": None,
            "raw_source_text": f"GovEase schedule row: {raw_line}",
            "raw_paragraph": raw_line,
        }
        return prop

    def _finalize_observed(self, rows: list[list[dict]]) -> None:
        """Record every TN target-county auction id seen this run."""
        observed: set[str] = set()
        for cells in rows:
            state = cells[1].get("t", "").strip().upper()
            if state != "TN":
                continue
            county = _normalize_county(cells[0].get("t", ""))
            if county is None:
                continue
            hrefs = cells[8].get("hrefs") or []
            slug, auction_id = _parse_liveauctions_url(
                hrefs[0] if hrefs else "", cells[0].get("t", ""))
            observed.add(f"{slug}-{auction_id}-{_slug_words(cells[7].get('t', ''))}")
        self.observed_listing_ids = observed


def _clean_city(city_raw: str) -> Optional[str]:
    """'location_on\\nDresden' -> 'Dresden'."""
    parts = [p.strip() for p in (city_raw or "").split("\n")]
    parts = [p for p in parts if p and p.lower() != "location_on"]
    return parts[0] if parts else None


def _slug_words(text: str) -> str:
    slug = (text or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "sale"


def _normalize_county(county_cell: str) -> Optional[str]:
    """Return the target-county display name, or None if outside scope.

    Handles multi-office labels ("Gibson County - Trenton Chancery",
    "Madison County Board of Commissioners"). The 38 target counties
    come from config.
    """
    from .config import TN_FORECLOSURE_COUNTIES
    text = (county_cell or "").strip()
    text = re.sub(r"\s+County\b.*$", "", text, flags=re.I)
    text = text.split(" - ")[0].strip()
    lc = text.lower()
    if lc in TN_FORECLOSURE_COUNTIES:
        return text.title()
    return None


def _parse_liveauctions_url(href: str, county_cell: str) -> tuple[str, str]:
    m = _ROW_RE.search(href or "")
    if m:
        return m.group("slug").lower(), m.group("auction_id")
    # No parcel-list link (the widget sometimes leaves it blank): synthesize
    # a stable id from the county label so the row still dedups/reaps.
    return _slug_words(county_cell), "0"


# ---------------------------------------------------------------------------

def main() -> int:  # pragma: no cover - direct-run helper
    props = GovEaseScraper().run()
    print(f"GovEase: {len(props)} target-county auctions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
