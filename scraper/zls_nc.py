"""Zacchaeus Legal Services (ZLS-NC) - NC Tax Foreclosure Listings scraper.

Scrapes zls-nc.com which uses DevExpress Blazor DataGrid with server-side
data source. Accepts consent via clicking "I AGREE" button, sets page size
to "All", then parses the full grid table.
"""
from __future__ import annotations
import logging
import re
from typing import Optional, Any

from .base import BaseScraper, PropertyData, camoufox_context
from .config import config, NC_FORECLOSURE_COUNTIES

logger = logging.getLogger(__name__)


class ZLSNCScraper(BaseScraper):
    """Scraper for ZLS-NC tax foreclosure listings via Playwright."""

    SOURCE_NAME = "zls_nc"
    BASE_URL = "https://zls-nc.com/listings"
    MIN_ACREAGE = config.MIN_ACRES

    def __init__(self, delay_range: tuple[float, float] = (2.0, 4.0)):
        super().__init__(delay_range=delay_range, use_selenium=False)

    def scrape(self) -> list[PropertyData]:
        all_properties: list[PropertyData] = []
        logger.info("Starting ZLS-NC scraper ...")

        with camoufox_context() as page:
            page.set_viewport_size({"width": 1920, "height": 1080})

            page.goto(self.BASE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # Accept consent
            agree_btn = page.locator("button:has-text('I AGREE')", has_not_text="I DO NOT")
            if agree_btn.count():
                logger.info("Accepting consent ...")
                agree_btn.click()
                page.wait_for_timeout(5000)

            # Set page size to All (single page load)
            dropdown_btn = page.locator('button[aria-label="Open or close the drop-down window"]').first
            if dropdown_btn.count():
                logger.info("Setting page size to All ...")
                dropdown_btn.click()
                page.wait_for_timeout(2000)
                all_opt = page.get_by_role("option", name="All").first
                if all_opt.count():
                    all_opt.click()
                    page.wait_for_timeout(2000)
                else:
                    logger.warning("Could not find 'All' option in dropdown")
            else:
                logger.warning("Could not find page size dropdown")

            # Verify table exists
            table = page.locator("table:has(th:has-text('Parcel #'))")
            if not table.count():
                logger.warning("No data grid found on %s", self.BASE_URL)
                return []

            rows = table.locator("tbody tr")
            total = 0
            try:
                total = rows.count()
            except Exception:
                pass
            logger.info("Found %d total rows", total)

            for i in range(total):
                try:
                    prop = self._parse_row(rows.nth(i))
                    if prop:
                        all_properties.append(prop)
                except Exception as exc:
                    logger.warning("Row %d error: %s", i, exc)
                page.wait_for_timeout(200)
                if (i + 1) % 50 == 0:
                    self._random_delay()

        # Deduplicate
        seen: set[str] = set()
        unique: list[PropertyData] = []
        for p in all_properties:
            pid = p.get("source_listing_id") or ""
            if pid and pid not in seen:
                seen.add(pid)
                unique.append(p)

        # Filter to NC mountain counties only
        logger.info("Filtering %d unique properties to NC mountain counties ...", len(unique))
        unique = self._filter_counties(unique)

        logger.info("ZLS-NC: %d raw -> %d unique after county filter", len(all_properties), len(unique))
        return unique

    def _filter_counties(self, properties: list[PropertyData]) -> list[PropertyData]:
        """Filter to NC mountain counties only."""
        NC_FORECLOSURE_COUNTIES = {
            "alleghany", "ashe", "avery", "buncombe", "burke",
            "cherokee", "clay", "graham", "haywood",
            "henderson", "jackson", "madison", "mcdowell", "mitchell",
            "swain", "transylvania", "watauga", "yancey",
            "polk", "macon",
        }
        
        filtered = []
        skipped = 0
        for prop in properties:
            county = prop.get("county") or ""
            if county.lower() in NC_FORECLOSURE_COUNTIES:
                filtered.append(prop)
            else:
                skipped += 1
        logger.info("ZLS-NC county filter: %d kept, %d skipped (non-mountain counties)", len(filtered), skipped)
        return filtered

    def _enrich_gis(self, properties: list[PropertyData]) -> list[PropertyData]:
        """Enrich properties with GIS acreage data using NC OneMap.

        Always returns all properties — GIS enrichment is optional.
        Properties without GIS data keep original parcel_number.
        """
        from scraper.nc_gis_lookup import NC1MapService

        service = NC1MapService()

        enriched_count = 0
        no_parcel_count = 0
        no_gis_count = 0

        for prop in properties:
            parcel = prop.get("parcel_number") or ""

            if not parcel:
                no_parcel_count += 1
                continue

            parcel_clean = parcel.strip().replace("-", "").upper()
            parcel_data = service.by_parcel(parcel_clean, timeout=10)

            if parcel_data:
                features = parcel_data.get("features", [])
                if features:
                    attrs = features[0].get("attributes", {})
                    acres = attrs.get("gisacres") or attrs.get("acres") or attrs.get("acreage")
                    if acres:
                        prop["acres"] = acres
                        prop["acres_source"] = "gis"
                        enriched_count += 1

            if not parcel_data:
                no_gis_count += 1

        logger.info("ZLS-NC GIS: kept %d, enriched %d (acres found), %d skipped (no parcel), %d failed GIS",
                    len(properties) - no_parcel_count, enriched_count, no_parcel_count, no_gis_count)
        return properties

    @staticmethod
    def _parse_row(row) -> Optional[PropertyData]:
        try:
            cells = row.locator("td")
            cc = cells.count()
            if cc < 5:
                return None

            values: list[str] = []
            for i in range(cc - 2):
                values.append(cells.nth(i).inner_text().strip())

            if len(values) < 7:
                return None

            tax_office, parcel = values[0], values[1]
            status, sale_date = values[2], values[3]
            upset_deadline, opening_bid = values[4], values[5]
            current_bid = values[6]

            addr_cell = cells.nth(cc - 1)
            address = re.sub(r'^\u26a0\ufe0f\s*', '', addr_cell.inner_text().strip()).strip()

            county = ZLSNCScraper._extract_county(tax_office)
            sale_clean = sale_date.strip() if sale_date and "not yet" not in sale_date.lower() else None

            gis_url = ZLSNCScraper._get_gis_url(county, parcel)
            maps_url = ZLSNCScraper._gm(address, county) if address or (parcel and county) else None
            topo_url = ZLSNCScraper._gmt(maps_url) if maps_url else (
                f"https://www.google.com/maps/search/parcel+{parcel}+in+{county}+NC/@?api=1&map_action=map&base=maps.terrain"
                if parcel and county else None
            )

            return {
                "source": "zls_nc",
                "source_listing_id": f"zls_nc_{parcel}" if parcel else "zls_nc_g",
                "url": "https://zls-nc.com/listings",
                "address": address if address else None,
                "city": None,
                "county": county,
                "state": "NC",
                "zip_code": None,
                "latitude": None,
                "longitude": None,
                "price": ZLSNCScraper._parse_price(opening_bid) or None,
                "acres": None,
                "acres_source": None,
                "description": ZLSNCScraper._desc(status, sale_date, upset_deadline, opening_bid, current_bid, parcel),
                "property_type": "tax_foreclosure",
                "image_url": None,
                "parcel_number": parcel or None,
                "gis_url": gis_url,
                "auction_date": sale_clean,
                "close_date": None,
                "upset_bid": upset_deadline or None,
                "foreclosure_key": f"{parcel}|{status}|{opening_bid}",
                "google_maps_url": maps_url,
                "google_maps_topo_url": topo_url,
            }
        except Exception:
            return None

    def _passes_filter(self, p: PropertyData) -> bool:
        return (p.get("acres") or 0) >= self.MIN_ACREAGE

    @staticmethod
    def _extract_county(to: str) -> Optional[str]:
        import re as _r
        m = _r.search(r'(\w+)\s+County\s+Tax\s+Office', to, _r.IGNORECASE)
        if m:
            return m.group(1).capitalize()
        m = _r.search(r'(\w+)\s+Tax\s+Office', to, _r.IGNORECASE)
        return m.group(1).capitalize() if m else None

    @staticmethod
    def _parse_price(t: str) -> Optional[int]:
        if not t or t.strip().lower() in ("n/a", "not yet set"):
            return None
        c = re.sub(r'[^\d.]', '', t)
        if not c:
            return None
        try:
            return int(float(c) * 100)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _desc(st, sd, ud, ob, cb, par):
        ps = []
        if st and "Not yet" not in st and "Pending" not in st:
            ps.append(f"Status: {st}")
        if ob and ob.lower() != "n/a":
            ps.append(f"Opening: {ob}")
        if cb and cb.lower() != "n/a":
            ps.append(f"Current: {cb}")
        if ud and ud != "n/a":
            ps.append(f"Upset: {ud}")
        if par:
            ps.append(f"Parcel: {par}")
        return " | ".join(ps) if ps else None

    @staticmethod
    def _get_gis_url(cty, parcel):
        """Get GIS URL for a county/parcel using county portal registry."""
        from scraper.gis_urls import get_gis_viewer_url
        return get_gis_viewer_url(cty, parcel) if parcel and cty else None

    @staticmethod
    def _gm(addr, cty):
        if not addr and not cty:
            return None
        parts = []
        if addr:
            parts.append(addr)
        if cty:
            parts.append(cty)
        if not parts:
            return None
        parts.append("NC")
        return f"https://www.google.com/maps/search/{'+'.join(parts)}"

    @staticmethod
    def _gmt(url):
        if url and "/search/" in url:
            return f"{url}/@?api=1&map_action=map&base=maps.terrain"
        return None
