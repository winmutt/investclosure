"""NC Foreclosure Notices scraper — ncnotices.com via Playwright + 2captcha.

Scrapes foreclosure public notices for 21 NC mountain counties.

Architecture:
    Playwright (headless Chromium) -> Proxy (if configured) -> ncnotices.com
    ASP.NET WebForms with session-based URLs and reCAPTCHA on detail pages.
    reCAPTCHA solved via 2captcha API.
"""
from __future__ import annotations
import re
import logging
from typing import Optional

from .base import BaseForeclosureScraper, PropertyData
from .config import (
    config,
    NCFORECLOSURES_BASE_URL,
    NCFORECLOSURES_CAPTCHA_SITE_KEY,
    NCFORECLOSURES_POPULAR_SEARCH_VALUE,
    NC_FORECLOSURE_COUNTIES,
)

logger = logging.getLogger(__name__)

COUNTY_SET = set(NC_FORECLOSURE_COUNTIES)


class NCForeclosureScraper(BaseForeclosureScraper):
    """Scraper for NC public foreclosure notices from ncnotices.com."""

    SOURCE_NAME = "ncforeclosures"
    BASE_URL = NCFORECLOSURES_BASE_URL

    def __init__(
        self,
        search_type: str = "foreclosure",
        delay: float = 1.5,
        use_proxy: bool = True,
        solve_captcha: bool = True,
    ):
        super().__init__(search_type=search_type, delay=delay,
                         use_proxy=use_proxy, solve_captcha=solve_captcha)

    def _get_target_counties(self) -> set[str]:
        return COUNTY_SET

    def scrape(self):  # -> List[PropertyData]
        """Run the scraper: search ncnotices.com and extract qualifying cases."""
        print(f"\n  NC FORECLOSURES - {len(COUNTY_SET)} target counties")
        print(f"  Search type: {self.search_type}")
        print(f"  Proxy: {'enabled' if self.use_proxy else 'disabled'}")
        print(f"  Captcha solving: {'enabled' if self.solve_captcha else 'disabled (search only)'}")
        print()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("playwright not installed — pip install playwright")
            return []

        properties = []

        chromium_path = self._find_chromium()
        proxy_cfg = {"server": config.PROXY_URL} if self.use_proxy and config.PROXY_URL else None

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=chromium_path,
                proxy=proxy_cfg,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })

            try:
                print("  [1/4] Connecting to ncnotices.com ...", end=" ", flush=True)
                page.goto(self.BASE_URL + "/", wait_until="networkidle", timeout=30000)
                session_id = self._extract_session(page.url)
                if not session_id:
                    logger.error("Could not extract session ID")
                    return []
                print(f"session={session_id}")

                print("  [2/4] Searching foreclosure notices ...", end=" ", flush=True)
                self._search_foreclosures(page)
                print("done")

                # Get total pages
                total_pages = int(page.evaluate("""() => {
                    const el = document.getElementById('ctl00_ContentPlaceHolder1_WSExtendedGridNP1_GridView1_ctl01_lblTotalPages');
                    const m = (el?.textContent || '').match(/of\\s+(\\d+)/);
                    return m ? parseInt(m[1]) : 1;
                }""") or 1)
                print(f"\n  Total pages: {total_pages}")

                print("  [3/4] Collecting records from all pages ...")
                all_records = []
                seen_pk = set()

                for page_num in range(1, total_pages + 1):
                    # Parse records from current page
                    records = self._parse_grid_records(page)
                    print(f"    Page {page_num}/{total_pages}: {len(records)} notices", end="", flush=True)

                    for r in records:
                        pk = r.get("pk_id")
                        if pk in seen_pk:
                            continue
                        seen_pk.add(pk)
                        if (r.get("county") or "").lower() in COUNTY_SET:
                            all_records.append(r)

                    print(f" (target: {len(all_records)})", flush=True)

                    # Stop if no more pages
                    if page_num >= total_pages:
                        break

                    # Navigate to next page
                    self._go_to_page(page, page_num + 1)
                    page.wait_for_timeout(8000)

                print(f"\n  Total qualifying notices: {len(all_records)}")

                print(f"  [4/4] Extracting details ({len(all_records)} cases) ...")
                for i, rec in enumerate(target_records):
                    print(f"    [{i+1}/{len(target_records)}] {rec['sp_case'] or rec['pk_id']} - {rec.get('county', '?')}",
                          end=" ", flush=True)
                    prop = self._extract_detail(page, session_id, rec)
                    if prop:
                        print("-> qualifying property")
                        properties.append(prop)
                    else:
                        print("(skipped)")

            finally:
                browser.close()

        return properties

    # ---- browser interactions ---------------------------------------------

    def _search_foreclosures(self, page) -> None:
        page.select_option(
            'select[name="ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"]',
            NCFORECLOSURES_POPULAR_SEARCH_VALUE,
        )
        page.wait_for_timeout(8000)

    def _go_to_page(self, page, page_num: int) -> None:
        """Navigate to a specific page number via ASP.NET postback."""
        page.evaluate(f"""() => {{
            const target = document.querySelector('input[name="__EVENTTARGET"]');
            const arg = document.querySelector('input[name="__EVENTARGUMENT"]');
            if (target && arg) {{
                target.value = 'ctl00$ContentPlaceHolder1$WSExtendedGridNP1$GridView1';
                arg.value = 'Page${page_num}';
                if (typeof __doPostBack === 'function') {{
                    __doPostBack('ctl00$ContentPlaceHolder1$WSExtendedGridNP1$GridView1', 'Page${page_num}');
                }}
            }}
        }}""")

    def _parse_grid_records(self, page):
        """Parse ASP.NET GridView rows into record dicts."""
        records = page.evaluate("""() => {
            const table = document.querySelector('table[id*="GridView"]');
            if (!table) return [];

            const allRows = Array.from(table.querySelectorAll('tr'));
            const seenPk = new Set();
            const results = [];

            allRows.forEach(function(row) {
                const hdn = row.querySelector('input[id*="hdnPKValue"]');
                if (!hdn) return;
                const pkId = hdn.value;
                if (seenPk.has(pkId)) return;
                seenPk.add(pkId);

                let texts = [row.textContent || ''];
                let sib = row.nextElementSibling;
                while (sib && sib.tagName === 'TR' && !sib.querySelector('input[id*="hdnPKValue"]')) {
                    texts.push(sib.textContent || '');
                    sib = sib.nextElementSibling;
                }
                const fullText = texts.join(' ').replace(/\\s+/g, ' ').trim();

                const spMatch = fullText.match(/\\b(2[456]SP\\d+[\\w-]*)\\b/);
                const spCase = spMatch ? spMatch[1] : null;

                let county = null;
                const cm1 = fullText.match(/NORTH\\s+CAROLINA\\s*[,:]?\\s*([A-Z\\s]+?)\\s+COUNTY/i);
                if (cm1) county = cm1[1].trim();
                if (!county) {
                    const cm2 = fullText.match(/(?:SUPERIOR|DISTRICT)\\s+Court\\s+DIVISION\\s+([A-Z][A-Za-z]+)\\s+COUNTY/i);
                    if (cm2) county = cm2[1].trim();
                }
                // "NORTH CAROLINA\\nCOUNTYNAME COUNTY" or "COUNTYNAME, NORTH CAROLINA" 
                if (!county) {
                    const cm3 = fullText.match(/NORTH\\s+CAROLINA[\\s,]+([A-Z][A-Za-z]+)[\\s,]+COUNTY/i);
                    if (cm3) county = cm3[1].trim();
                }

                const btn2 = row.querySelector('input[id*="btnView2"]');
                let detailUrl = null;
                if (btn2) {
                    const onclick = btn2.getAttribute('onclick') || '';
                    const urlMatch = onclick.match(/location\\.href='([^']+)'/);
                    if (urlMatch) detailUrl = urlMatch[1];
                }

                results.push({
                    pk_id: pkId,
                    sp_case: spCase,
                    county: county,
                    detail_url: detailUrl,
                });
            });
            return results;
        }""")
        return records  # List[dict]

    def _extract_detail(self, page, session_id: str, record: dict) -> Optional[PropertyData]:
        """Navigate to detail page, solve captcha if needed, extract notice text."""
        pk_id = record["pk_id"]
        detail_url = f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}"

        try:
            page.goto(detail_url, wait_until="load", timeout=60000)
        except Exception as e:
            logger.warning("Failed to load detail page: %s", e)
            return None

        # Wait for ASP.NET AJAX content to render
        page.wait_for_timeout(5000)

        # Verify content loaded by checking for notice-related text
        page_text = page.evaluate("() => document.body.innerText")
        if len(page_text) < 500:
            page.wait_for_timeout(5000)
            page_text = page.evaluate("() => document.body.innerText")
            if len(page_text) < 500:
                logger.warning("Content too short (%d chars), page may not have loaded: %s", len(page_text), pk_id)
                return None

        has_captcha = page.evaluate(
            "() => !!document.getElementById('g-recaptcha-response')"
        )

        if has_captcha and self.solve_captcha:
            token = self._solve_captcha(detail_url, NCFORECLOSURES_CAPTCHA_SITE_KEY)
            if not token:
                logger.warning("Captcha solve failed for %s", pk_id)
                return None
            self._inject_token_and_submit(page, token)
            page.wait_for_timeout(5000)

            still_blocked = page.evaluate(
                "() => !!document.getElementById('ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_lblMessage')"
            )
            if still_blocked:
                logger.warning("Still blocked after captcha for %s", pk_id)
                return None

        notice_text = page.evaluate("() => document.body.innerText")
        acres = self._extract_acreage(notice_text)

        if acres is not None and acres < config.MIN_ACRES:
            return None

        prop: PropertyData = {
            "source": self.SOURCE_NAME,
            "source_listing_id": record.get("sp_case") or pk_id,
            "url": detail_url,
            "address": None,
            "city": None,
            "county": (record.get("county") or "").lower().strip(),
            "state": "NC",
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": 1,
            "acres": acres,
            "description": notice_text[:2000],
            "property_type": "foreclosure",
            "image_url": None,
        }
        return prop


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def scrape_all() -> list[PropertyData]:
    return NCForeclosureScraper().run()
