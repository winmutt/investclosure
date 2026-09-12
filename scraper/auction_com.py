"""Auction.com trustee-sale + bank-owned scraper (Mtg tab).

Auction.com exposes per-county pages (/residential/<ST>/<county>-county)
with asset cards linking to rich detail pages (address, county, beds/baths,
lot acres, APN parcel, trustee/case numbers, auction date, opening bid,
Foreclosure-Sale vs Bank-Owned label). No bot wall; server-rendered.

Strategy (bounded): county grids enumerate asset IDs; detail pages are
visited only for assets not already in the DB (known assets get a
last_seen touch so they don't go stale). Filtered to the mountain-county
sets of NC/GA/TN/SC/AL. Private-Seller listings are skipped.
"""
from __future__ import annotations
import logging
import re
import time
from datetime import date
from typing import Dict, List, Optional, Tuple

from .base import PropertyData, camoufox_context
from .config import (
    GA_MOUNTAIN_COUNTIES,
    NC_MOUNTAIN_COUNTIES,
    TN_FORECLOSURE_COUNTIES,
    QUALIFYING_COUNTIES,
)
from .trustee_base import (
    TrusteeSaleScraper, clean_html, parse_sale_date, price_to_dollars,
    split_address,
)

logger = logging.getLogger(__name__)

STATE_COUNTIES: Dict[str, set[str]] = {
    "NC": set(NC_MOUNTAIN_COUNTIES),
    "GA": set(GA_MOUNTAIN_COUNTIES),
    "TN": set(TN_FORECLOSURE_COUNTIES),
    "SC": set(QUALIFYING_COUNTIES.get("SC", [])),
    "AL": set(QUALIFYING_COUNTIES.get("AL", [])),
}
STATE_NAMES = {"NC": "North Carolina", "GA": "Georgia", "TN": "Tennessee",
               "SC": "South Carolina", "AL": "Alabama"}
MAX_PAGES_PER_COUNTY = 10
_ASSET_RE = re.compile(r"/details/.*?-(\d+)(?:[/?#]|$)")
_ASSET_FALLBACK_RE = re.compile(r"/details/.*?(\d{5,})(?:[/?#]|$)")


def county_slug(county: str) -> str:
    """'Buncombe' -> 'buncombe-county'; 'De Kalb' -> 'de-kalb-county'."""
    slug = re.sub(r"\s+", "-", (county or "").strip().lower())
    return f"{slug}-county"


def asset_id(url: str) -> Optional[str]:
    m = _ASSET_RE.search(url or "")
    if m:
        return m.group(1)
    m = _ASSET_FALLBACK_RE.search(url or "")
    return m.group(1) if m else None


class AuctionComScraper(TrusteeSaleScraper):
    SOURCE_NAME = "auction_com"
    STATE = ""  # multi-state; set per row
    TARGET_COUNTIES = set()
    LIST_URL = "https://www.auction.com/"

    def scrape(self) -> List[PropertyData]:
        props: List[PropertyData] = []
        try:
            with camoufox_context() as page:
                # Bound every page call: sync evaluate() takes no timeout
                # kwarg in this Playwright version, so the default covers
                # hung renderers instead (killed a sweep on Union, 2026-09-12).
                page.set_default_timeout(self.DEFAULT_TIMEOUT_MS)
                known = self._known_assets()
                for state, counties in STATE_COUNTIES.items():
                    for county in sorted(counties):
                        props.extend(self._scrape_county(page, state, county,
                                                         known))
                        time.sleep(2)
        except Exception as e:
            logger.error("AuctionCom fetch failed: %s", e, exc_info=True)
        return props

    # -- inventory ---------------------------------------------------

    def _known_assets(self) -> set[str]:
        """Asset IDs already stored (skip detail re-visits for these)."""
        try:
            import sqlite3
            from .config import config
            conn = sqlite3.connect(str(config.db_path))
            try:
                rows = conn.execute(
                    "SELECT source_listing_id FROM properties "
                    "WHERE source = ?", (self.SOURCE_NAME,)).fetchall()
            finally:
                conn.close()
            return {r[0] for r in rows if r[0]}
        except Exception:
            return set()

    def _touch_seen(self, asset_ids: List[str]) -> None:
        """Refresh last_seen for already-stored assets (no field changes)."""
        if not asset_ids:
            return
        try:
            import sqlite3
            from datetime import date as _date
            from .config import config
            conn = sqlite3.connect(str(config.db_path))
            try:
                conn.executemany(
                    "UPDATE properties SET last_seen = ? "
                    "WHERE source = ? AND source_listing_id = ?",
                    [(_date.today().isoformat(), self.SOURCE_NAME, a)
                     for a in asset_ids],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("AuctionCom touch failed: %s", e)

    # -- county grid -------------------------------------------------

    # Bound for every page call (see scrape): a wedged renderer hangs
    # untimed calls forever (killed a full sweep on Union county, 2026-09-12).
    DEFAULT_TIMEOUT_MS = 25000

    def _pager_hrefs(self, page) -> List[str]:
        try:
            return page.evaluate("""() => Array.from(document.querySelectorAll('a'))
              .map(a => ({t: (a.innerText || '').trim(), h: a.href || ''}))
              .filter(x => /^(\\d+|Next|›|»)$/.test(x.t) && x.h
                && !x.h.includes('/details/'))
              .map(x => x.h)""") or []
        except Exception:
            return []

    def _county_assets(self, page, state: str, county: str) -> Tuple[List[str], int]:
        """(asset detail URLs, native-count) across all pager pages."""
        url = (f"https://www.auction.com/residential/{state}/"
               f"{county_slug(county)}")
        found: List[str] = []
        native = -1  # unknown until the count line parses; skip only on == 0
        seen_pages: set[str] = set()
        for _ in range(MAX_PAGES_PER_COUNTY):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
            except Exception as e:
                logger.warning("AuctionCom county load failed %s %s: %s",
                               state, county, e)
                break
            if state.lower() not in (page.url or "").lower():
                logger.warning("AuctionCom unexpected landing for %s %s: %s",
                               state, county, page.url)
                break
            seen_pages.add(url)
            try:
                hrefs = page.evaluate(
                    "() => Array.from(document.querySelectorAll("
                    "'a[href*=\"/details/\"]')).map(a => a.href)") or []
                if native < 0:
                    count_text = page.inner_text("body") or ""
                    native = self._native_count(count_text, county, state)
            except Exception as e:
                logger.warning("AuctionCom grid read failed %s %s: %s",
                               state, county, str(e)[:150])
                break
            for h in hrefs:
                if h not in found:
                    found.append(h)
            nxt = next((h for h in self._pager_hrefs(page) if h not in seen_pages), None)
            if not nxt:
                break
            url = nxt
            time.sleep(2)
        return found, native

    @staticmethod
    def _native_count(body: str, county: str, state: str) -> int:
        """'5 Properties in Buncombe County, NC' -> 5 (-1 when unparseable)."""
        m = re.search(
            r"([\d,]+)\s+Properties\s+in\s+" + re.escape(county) + r"\s+County,\s*" + state,
            body or "", re.IGNORECASE)
        if not m:
            return -1
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return -1

    def _scrape_county(self, page, state: str, county: str,
                       known: set[str]) -> List[PropertyData]:
        props: List[PropertyData] = []
        hrefs, native = self._county_assets(page, state, county)
        if not hrefs:
            return props
        fresh, seen = [], []
        for h in hrefs:
            aid = asset_id(h)
            if not aid:
                continue
            (fresh if aid not in known else seen).append((aid, h))
        self._touch_seen([a for a, _ in seen])
        if native == 0 and fresh:
            # Page shows only nearby-county spillover; those assets belong
            # to their home counties' pages (visited separately).
            logger.info("AuctionCom %s %s: %d nearby-only, skipping details",
                        state, county, len(fresh))
            return props
        total_fresh = len(fresh)
        for i, (aid, href) in enumerate(fresh):
            if (i + 1) % 10 == 0:
                print(f"    ... {i + 1}/{total_fresh} details ({county})",
                      flush=True)
            try:
                prop = self._scrape_detail(page, state, county, aid, href)
            except Exception as e:
                logger.warning("AuctionCom detail failed %s: %s", href, e)
                prop = None
            if prop:
                props.append(prop)
                known.add(aid)
            time.sleep(2)
        if fresh or seen:
            logger.info("AuctionCom %s %s: %d new, %d seen",
                        state, county, len(props), len(seen))
        return props

    # -- detail ------------------------------------------------------

    # Sale-type label bound directly to the address line. (The nav bar also
    # contains these words, so the label must be matched jointly with the
    # address it precedes — never standalone.)
    _LABEL_ADDR_RE = re.compile(
        r"(Foreclosure Sale|Bank Owned|Private Seller)\s*\|\s*"
        r"([^|,]+?)\s*\|\s*([^|,]+?),\s*([A-Z]{2})\s+(\d{5}),\s*"
        r"([A-Za-z][A-Za-z .'-]*?)\s+County", re.IGNORECASE)
    _FIELD_RES = {
        "beds": r"\|\s*([\d.]+)\s*\|\s*Beds?\b",
        "baths": r"\|\s*([\d.]+)\s*\|\s*Baths?\b",
        "sqft": r"\|\s*([\d,]+)\s*\|\s*Sq\.?\s*Feet\b",
        "acres": r"Lot Size\s*\(Acres\)\s*\|\s*([\d,.]+)",
        "parcel": r"\bAPN\s*\|\s*([A-Za-z0-9\- ]+?)(?:\s*\||$)",
        "trustee_no": r"Trustee Sale Number\s*\|\s*([A-Za-z0-9\-# ]+?)(?:\s*\||$)",
        "sp_case": r"Special Proceedings ID\s*\|\s*([A-Za-z0-9\- ]+?)(?:\s*\||$)",
        "sale_date": r"\bDate\s*\|\s*([A-Za-z]+,?\s+[A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        "bid": r"(\$[\d,]+|TBD)\s*\|\s*Opening Bid",
        "occupancy": r"\b(VACANT|OCCUPIED)\b",
    }

    def _scrape_detail(self, page, state: str, county: str,
                       aid: str, href: str) -> Optional[PropertyData]:
        page.goto(href, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        try:
            body = page.inner_text("body") or ""
        except Exception as e:
            logger.warning("AuctionCom detail read failed %s: %s",
                           href, str(e)[:150])
            return None
        if len(body) < 500:
            return None
        return self._parse_detail(body, href, aid, state, county)

    def _parse_detail(self, body: str, href: str, aid: str,
                      state: str, county: str) -> Optional[PropertyData]:
        """Parse a detail body (pure; unit-testable)."""
        # innerText separates blocks with newlines; normalize to pipes so the
        # field patterns below match live pages and fixtures alike.
        text = re.sub(r"[ \t]*\n[ \t]*", " | ", body or "")
        m = self._LABEL_ADDR_RE.search(text)
        if not m:
            return None
        kind = m.group(1).strip().lower()
        if "private seller" in kind:
            return None
        ptype = ("bank_owned" if "bank owned" in kind
                 else "mortgage_foreclosure")
        street = clean_html(m.group(2))
        city = clean_html(m.group(3))
        st = m.group(4).strip().upper()
        zip_code = m.group(5).strip()
        det_county = clean_html(m.group(6))
        if st != state or det_county.strip().lower() != county.strip().lower():
            return None

        f: Dict[str, str] = {}
        for key, pat in self._FIELD_RES.items():
            mm = re.search(pat, text, re.IGNORECASE)
            f[key] = clean_html(mm.group(1)) if mm else ""
        # Skip off-market / coming-soon shells with no sale or bid signal.
        sale_date = parse_sale_date(f["sale_date"])
        today = date.today().isoformat()
        if sale_date is not None and sale_date < today:
            return None
        court_case = None
        if f["sp_case"]:
            cm = re.search(r"\d{2}\s?(?:SP|CV)\s?\d+(?:-\d+)?", f["sp_case"], re.IGNORECASE)
            court_case = cm.group(0).replace(" ", "") if cm else None
        try:
            acres = float(f["acres"].replace(",", "")) if f["acres"] else None
        except ValueError:
            acres = None
        sqft = f["sqft"].replace(",", "") if f["sqft"] else ""
        desc = (f"[Auction.com] {kind.title()} | {street}, {city} {zip_code} | "
                f"Sale: {f['sale_date'] or 'TBD'} | Bid: {f['bid'] or 'TBD'} | "
                f"{f['beds'] + 'bd/' if f['beds'] else ''}"
                f"{f['baths'] + 'ba' if f['baths'] else ''} "
                f"{sqft + ' sqft' if sqft else ''} "
                f"{f['occupancy'] + ' ' if f['occupancy'] else ''}"
                f"{('APN ' + f['parcel']) if f['parcel'] else ''} "
                f"{('Trustee# ' + f['trustee_no']) if f['trustee_no'] else ''}".strip())
        prop = self.build_property(
            listing_id=aid,
            county=det_county,
            state=state,
            address=street or None,
            city=city or None,
            zip_code=zip_code or None,
            acres=acres,
            parcel_number=f["parcel"] or None,
            court_case=court_case,
            sale_date=sale_date,
            price_dollars=price_to_dollars(f["bid"]),
            description=desc,
            raw_text=body,
            url=href,
        )
        prop["property_type"] = ptype
        return prop


def scrape_all() -> List[PropertyData]:
    return AuctionComScraper().scrape()
