"""Newspaper public notices scraper -- local mountain county newspapers.

Scrapes 4 NC mountain county newspapers for public/legal notices:
  - Transylvania Times (Brevard, transylvania)  -- AdPerfect platform
  - Watauga Democrat (Boone, watauga)            -- BLOX/CMX platform
  - Sylva Herald (Sylva, jackson)                -- BLOX/CMX platform
  - Mitchell News (Spruce Pine, mitchell)        -- BLOX/CMX platform

These sites publish classified notices including foreclosures, tax lien sales,
trustee sales, real estate sales, estate proceedings, and legal filings.

Architecture: 2-phase per scraper
  Phase 1 -- listing page: collect URLs + basic metadata (no navigation away)
  Phase 2 -- detail pages: visit each URL in an isolated tab, extract parcel#
"""
from __future__ import annotations

import logging
import time
import re
import time
from typing import Any, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from .base import BaseScraper, PropertyData
from .config import config, NC_FORECLOSURE_COUNTIES

logger = logging.getLogger(__name__)

# NC mountain counties we care about
NC_FORECLOSURE_COUNTIES = {
    "alleghany", "ashe", "avery", "buncombe", "burke",
    "caldwell", "cherokee", "clay", "graham", "haywood",
    "henderson", "jackson", "madison", "mcdowell", "mitchell",
    "polk", "macon", "swain", "transylvania", "watauga", "yancey",
}

# Slug patterns that indicate real-property-relevant notices
PROPERTY_RELEVANT_SLUGS = [
    "foreclosure", "sale", "exchange", "trust", "lien", "mortgage",
    "distress", "sheriff", "execution", "attachment", "judicial",
    "creditor", "filing", "publication", "bidding",
]


def _slug_to_title(slug: str) -> str:
    if not slug:
        return "Unknown"
    return " ".join(slug.replace("-", " ").title().split())


class NewspaperNoticesScraper(BaseScraper):
    """Scraper for newspaper public notices via Playwright."""

    SOURCE_NAME = "newspaper_notices"

    def __init__(self, delay_range: tuple[float, float] = (2.0, 4.0)):
        super().__init__(delay_range=delay_range, use_selenium=False)

    def scrape(self) -> list[PropertyData]:
        all_properties: list[PropertyData] = []
        for scrape_fn in [
            self._scrape_transylvanian_times,
            self._scrape_watauga_democrat,
            self._scrape_sylvaherald,
            self._scrape_mitchellnews,
        ]:
            try:
                props = scrape_fn()
                all_properties.extend(props)
            except Exception as e:
                logger.warning("%s failed: %s", scrape_fn.__name__, e)

        # Deduplicate by source + URL
        seen: set[str] = set()
        unique: list[PropertyData] = []
        for p in all_properties:
            url = p.get("url") or ""
            if url and url not in seen:
                seen.add(url)
                unique.append(p)
        logger.info("Newspaper notices: %d raw -> %d unique", len(all_properties), len(unique))
        return unique

    def _passes_filter(self, p: PropertyData) -> bool:
        return True

    def run(self) -> list[PropertyData]:
        properties = self.scrape()
        filtered, skipped = [], 0
        for prop in properties:
            county = (prop.get("county") or "").lower()
            if county in NC_FORECLOSURE_COUNTIES:
                filtered.append(prop)
            else:
                skipped += 1
        logger.info("Newspaper county filter: %d kept, %d skipped", len(filtered), skipped)
        return filtered

    # ── Parcel extraction helpers ──────────────────────────────────────────

    def _extract_parcel(self, text: str) -> Optional[str]:
        """Return first parcel/REID/PIN/PID/Plat/Deed reference found in text."""
        # Tax parcel: "Watauga County tax parcel #1984-32-8523-000"
        m = re.search(r'[Tt]ax\s+parcel\s*#\s*(\d{4,5}-[\d\u2013\-]+)', text)
        if m:
            return m.group(1)

        # Generic parcel: "Parcel #1984-32-8523-000"  |  "Parcel 1984 32 8523 000"
        m = re.search(r'[Pp]arcel\s*[#\s]\s*(\d{3,}-[\d\u2013\-]+)', text)
        if m:
            return m.group(1)
        m = re.search(r'[Pp]arcel\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\u2013\-]{5,})', text)
        if m:
            return m.group(1)

        # REID (Real Estate ID -- NC tax deed)
        m = re.search(r'REID\s*[:#]?\s*(\d{4,})', text, re.IGNORECASE)
        if m:
            return m.group(1)

        # PIN / PID -- Parcel Identification No / Parcel ID
        # Hyphen-separated: PIN: 1984-42-0606-000
        m = re.search(r'(?:Parcel\s+ID\s+PIN?)\s*[:#]?\s*(\d{3,}-[\d\u2013\-]+)', text, re.IGNORECASE)
        if not m:
            m = re.search(r'(?:(?:Parcel\s+ID\s+)?PIN|PID)\s*[:#]?\s*(\d{3,}-[\d\u2013\-]+)', text, re.IGNORECASE)
        if not m:
            # Space-separated: PIN: 1984 42 0606 000 (10+ digits)
            m = re.search(r'(?:(?:Parcel\s+ID\s+)?PIN|PID)\s*[:#]?\s*(\d{4}\s+\d+\s+\d+\s+\d+)', text, re.IGNORECASE)
        if m:
            return re.sub(r'\s+', '-', m.group(1))

        # Plato / Plat Book + Page: "Plato Book 238 Page 10"
        m = re.search(r'(?:Plat(o)?)\s+(?:Book|Bk)\s+(\d+),?\s+(?:Page|Pg)\s+(\d+)', text, re.IGNORECASE)
        if m:
            return f"Plato:Bk{m.group(2)}Pg{m.group(3)}"

        # Deed Book + Page: "Deed Book 1234, Page 567" / "Deed Vol 1234 Page 567"
        m = re.search(r'(?:Deed)\s+(?:Book|Bk|Vol\.?|Volume)\s+(\d+),?\s+(?:Page|Pg)\s+(\d+)', text, re.IGNORECASE)
        if m:
            return f"Deed:Bk{m.group(1)}Pg{m.group(2)}"

        return None

    # ── Phase-2 helper: visit a single detail URL in an isolated tab ───────

    def _visit_detail(self, url: str) -> dict:
        """Open *url* in a fresh tab and return {parcel, title}."""
        parcel, title = None, None
        bw = sync_playwright().start()
        try:
            browser = bw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Wait until body has meaningful content
                page.wait_for_function(
                    "() => { const b = document.querySelector('body'); return b && b.innerText && b.innerText.length > 500; }",
                    timeout=10000,
                )

                # parcel text
                body_text = page.inner_text("body") or ""
                # Skip error/malformed pages, then extract parcel
                parcel = self._extract_parcel(body_text) if (body_text and not any(phrase in body_text.lower() for phrase in ["sorry", "error", "blocked", "404"])) else None

                # better title from heading
                for selector, tag in [("h1", "h1"), ("h2", "h2"), ('[class*="article-title"]', "text")]:
                    try:
                        el = page.locator(selector)
                        if el.count() > 0:
                            title = el.first.inner_text().strip()
                            break
                    except Exception:
                        pass
                if not title:
                    raw = page.evaluate("() => document.querySelector('head title')?.innerText || ''")
                    if raw:
                        title = raw.strip().split("|")[0].strip()
            except Exception as exc:
                logger.debug("detail load failed %s: %s", url[:80], exc)
        finally:
            try:
                browser.close()
            except Exception:
                pass
            bw.stop()

        return {"parcel": parcel, "title": title}

    # ── Phase-1 scrapers: return list of (url, card_data) from listing page ─

    def _scrape_transylvanian_times(self) -> list[PropertyData]:
        logger.info("Scraping Transylvania Times ...")
        url = "https://marketplace.transylvaniatimes.com/brevard-nc/public-notices/search"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            links = page.locator('[data-id] a[href*="/public-notices/"]')
            candidates: list[dict] = []
            for i in range(min(links.count(), 50)):
                try:
                    href = links.nth(i).get_attribute("href") or ""
                    parent = links.nth(i).evaluate_handle("el => el.closest('[data-id]')")
                    if not parent:
                        continue
                    card_text = parent.inner_text() or ""
                    lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                    title = lines[0] if lines else ""
                    content = " ".join(lines[1:]) if len(lines) > 1 else ""
                    date_match = [l for l in reversed(lines) if "Posted" in l or "Updated" in l]
                    posted = date_match[0].replace("Posted ", "").replace("Updated ", "").strip() if date_match else ""
                    if not href or "search" in href.lower():
                        continue
                    if not any(kw in title.upper() for kw in ("NOTICE","FORECLOS","SALE","CREDITOR","BIDDER")):
                        continue
                    combined = f"{title} {content}".lower()
                    if not any(pat in combined for pat in PROPERTY_RELEVANT_SLUGS):
                        continue
                    candidates.append({"href": href, "title": title, "content": content, "posted": posted})
                except Exception:
                    pass
                page.wait_for_timeout(200)
            browser.close()

        # Phase 2: visit each detail in its own tab
        properties: list[PropertyData] = []
        for c in candidates:
            d_url = urljoin(url, c["href"])
            time.sleep(1)
            detail = self._visit_detail(d_url)
            if detail.get("parcel") is None and not detail.get("title"):
                logger.warning("TT %s detail page failed or empty", c["href"][:40])
                continue
            ad_id = c["href"].split("/")[-1] if "/" in c["href"] else f"tt_skip"
            base_title = detail["title"] if detail["title"] and len(detail["title"]) > 5 else c["title"]
            desc = f"[Transylvania Times] {base_title}"
            if c["posted"]:
                desc += f" -- {c['posted']}"
            properties.append({
                "source": "newspaper_notices",
                "source_listing_id": f"tt_{ad_id}",
                "url": d_url,
                "address": None, "city": "Brevard", "county": "Transylvania", "state": "NC",
                "zip_code": None, "latitude": None, "longitude": None,
                "price": None, "acres": None,
                "description": desc,
                "property_type": "public_notice", "image_url": None,
                "parcel_number": detail["parcel"],
                "auction_date": c["posted"] or None, "close_date": None,
            })
        logger.info("Transylvania Times: %d relevant notices", len(properties))
        return properties

    def _scrape_watauga_democrat(self) -> list[PropertyData]:
        logger.info("Scraping Watauga Democrat ...")
        base = "https://www.wataugademocrat.com/classifieds/community/public_notices/"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            page.goto(base, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            html = page.content()

            ad_links = re.findall(r'/classifieds/community/public_notices/([^"\'<>]+)/ad_([0-9a-f-]+)\.html', html)
            all_dates = re.findall(r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4}', html)
            candidates: list[dict] = []
            seen: set[str] = set()
            for slug, uuid in ad_links[:25]:
                if uuid in seen:
                    continue
                seen.add(uuid)
                if not any(pat in slug.lower() for pat in PROPERTY_RELEVANT_SLUGS):
                    continue
                idx = len(candidates)
                date = all_dates[idx % len(all_dates)] if all_dates else None
                candidates.append({"uuid": uuid, "slug": slug, "date": date})
            browser.close()

        # Phase 2
        properties: list[PropertyData] = []
        for c in candidates:
            d_url = f"{base}{c['slug']}/ad_{c['uuid']}.html"
            time.sleep(2)
            detail = self._visit_detail(d_url)
            if detail.get("parcel") is None and not detail.get("title"):
                logger.warning("WD %s detail page failed or empty", c['slug'])
                continue
            base_title = _slug_to_title(c['slug'])
            if detail.get("title") and len(detail["title"]) > 5:
                base_title = detail["title"]
            desc = f"[Watauga Democrat] {base_title}"
            if c["date"]:
                desc += f" -- {c['date']}"
            properties.append({
                "source": "newspaper_notices",
                "source_listing_id": f"wd_{c['uuid']}",
                "url": d_url,
                "address": None, "city": "Boone", "county": "Watauga", "state": "NC",
                "zip_code": None, "latitude": None, "longitude": None,
                "price": None, "acres": None,
                "description": desc,
                "property_type": "public_notice", "image_url": None,
                "parcel_number": detail["parcel"],
                "auction_date": c["date"], "close_date": None,
            })
        logger.info("Watauga Democrat: %d relevant notices", len(properties))
        return properties

    def _scrape_sylvaherald(self) -> list[PropertyData]:
        logger.info("Scraping Sylva Herald ...")
        base = "https://www.thesylvaherald.com/classifieds/community/announcements/legal/"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            page.goto(base, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            html = page.content()

            # Capture full path including .html extension
            ids = re.findall(r'/classifieds/community/announcements/legal/((?:ad_)?[0-9a-f-]+\.html)', html)
            all_dates = re.findall(r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4}', html)
            candidates: list[dict] = []
            seen: set[str] = set()
            for uid in ids[:25]:
                if uid in seen:
                    continue
                seen.add(uid)
                idx = len(candidates)
                date = all_dates[idx % len(all_dates)] if all_dates else None
                candidates.append({"uid": uid, "date": date})
            browser.close()

        # Phase 2
        properties: list[PropertyData] = []
        for c in candidates:
            d_url = f"{base}{c['uid']}"  # uid already includes ad_ and .html
            time.sleep(2)
            detail = self._visit_detail(d_url)
            if detail.get("parcel") is None and not detail.get("title"):
                logger.warning("SH %s detail page failed or empty", c['uid'][:20])
                continue
            base_title = detail.get("title", "Legal Notice")
            if not base_title or len(base_title) < 4:
                base_title = "Legal Notice"
            desc = f"[Sylva Herald] {base_title}"
            if c["date"]:
                desc += f" -- {c['date']}"
            properties.append({
                "source": "newspaper_notices",
                "source_listing_id": f"sh_{c['uid']}",
                "url": d_url,
                "address": None, "city": "Sylva", "county": "Jackson", "state": "NC",
                "zip_code": None, "latitude": None, "longitude": None,
                "price": None, "acres": None,
                "description": desc,
                "property_type": "public_notice", "image_url": None,
                "parcel_number": detail["parcel"],
                "auction_date": c["date"], "close_date": None,
            })
        logger.info("Sylva Herald: %d legal notices", len(properties))
        return properties

    def _scrape_mitchellnews(self) -> list[PropertyData]:
        logger.info("Scraping Mitchell News ...")
        url = "https://www.mitchellnews.com/classified/legals"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(10000)
                body = page.inner_text("body") or ""
                if len(body) < 1500:
                    logger.info("Mitchell News: minimal content (%d chars), likely login wall", len(body))
                    return []
                html = page.content()
                ids = re.findall(r'/classified/legals/(?:ad_)?([0-9a-f-]+)', html)
                all_dates = re.findall(r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4}', html)
                candidates: list[dict] = []
                seen: set[str] = set()
                for uid in ids[:15]:
                    if uid in seen:
                        continue
                    seen.add(uid)
                    idx = len(candidates)
                    date = all_dates[idx % len(all_dates)] if all_dates else None
                    candidates.append({"uid": uid, "date": date})
            except Exception as exc:
                logger.warning("Mitchell News page load failed: %s", exc)
                return []
            finally:
                browser.close()

        # Phase 2
        properties: list[PropertyData] = []
        for c in candidates:
            time.sleep(0.5)
            detail = self._visit_detail(url)  # Mitchell has no per-notice URLs
            desc = f"[Mitchell News] Legal Notice"
            if c["date"]:
                desc += f" -- {c['date']}"
            properties.append({
                "source": "newspaper_notices",
                "source_listing_id": f"mn_{c['uid']}",
                "url": url,
                "address": None, "city": "Spruce Pine", "county": "Mitchell", "state": "NC",
                "zip_code": None, "latitude": None, "longitude": None,
                "price": None, "acres": None,
                "description": desc,
                "property_type": "public_notice", "image_url": None,
                "parcel_number": detail["parcel"],
                "auction_date": c["date"], "close_date": None,
            })
        logger.info("Mitchell News: %d legal notices", len(properties))
        return properties
