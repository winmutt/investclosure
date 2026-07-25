from __future__ import annotations
import logging
import re
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional, TypedDict, List

from .config import config

logger = logging.getLogger(__name__)


class PropertyData(TypedDict, total=False):
    """Common property data structure."""
    source: str
    source_listing_id: Optional[str]
    url: Optional[str]
    address: Optional[str]
    city: Optional[str]
    county: Optional[str]
    state: Optional[str]
    zip_code: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    price: Optional[float]
    acres: Optional[float]
    description: Optional[str]
    property_type: Optional[str]
    image_url: Optional[str]
    parcel_number: Optional[str]
    auction_date: Optional[str]
    close_date: Optional[str]
    upset_bid: Optional[str]
    foreclosure_key: Optional[str]


class BaseForeclosureScraper(ABC):
    """Base class for ASP.NET-based foreclosure notice scrapers (ncnotices.com, tnpublicnotice.com)."""

    SOURCE_NAME: str = "unknown"
    BASE_URL: str = ""

    def __init__(
        self,
        search_type: str = "foreclosure",
        delay: float = 1.5,
        use_proxy: bool = True,
        solve_captcha: bool = True,
    ):
        self.search_type = search_type
        self.use_proxy = use_proxy
        self.solve_captcha = solve_captcha
        self.delay = delay
        self.delay_range = (delay, delay * 2)
        self._request_count = 0
        self._last_request_time = time.time()

    # ---- public -----------------------------------------------------------

    def scrape(self) -> List[PropertyData]:
        """Override in subclass. Main scraping entry point."""
        raise NotImplementedError

    def run(self) -> List[PropertyData]:
        """Run scraper, filter results, return qualifying properties."""
        # Validate required dependencies at runtime
        if not config.TWO_CAPTCHA_API_KEY:
            print(
                f"\n{'!' * 70}\n"
                f"  FATAL: TWO_CAPTCHA_API_KEY is not set.\n"
                f"  Set it in your .env file or as an environment variable.\n"
                f"{'!' * 70}\n",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)

        state_counties = self._get_target_counties()
        count = len(state_counties)
        print(f"\n{'='*60}")
        print(f"  Running {self.SOURCE_NAME} scraper")
        print(f"  Target: {count} counties, >={config.MIN_ACRES:.0f}ac")
        print(f"{'='*60}")

        try:
            properties = self.scrape()
            print(f"\n  Total found: {len(properties)}")

            # Filter by county and acreage
            filtered = []
            skipped = 0
            for prop in properties:
                county = (prop.get("county") or "").lower().strip()
                acres = prop.get("acres")

                if county and county in state_counties:
                    if acres and acres >= config.MIN_ACRES:
                        prop["county"] = county.title()
                        filtered.append(prop)
                    else:
                        skipped += 1
                else:
                    skipped += 1

            print(f"  After filtering: {len(filtered)} qualifying, {skipped} skipped")
            return filtered

        except Exception as e:
            logger.error("Scraper %s failed: %s", self.SOURCE_NAME, e, exc_info=True)
            return []

    # ---- helpers ----------------------------------------------------------

    @abstractmethod
    def _get_target_counties(self) -> set[str]:
        """Return set of lowercase county names for this scraper."""
        raise NotImplementedError

    def _extract_acreage(self, text: str) -> Optional[float]:
        """Extract acreage from notice text."""
        patterns = [
            r"(?:containing|being|consisting of|contain)\s+approximately?\s+([\d,]+(?:\.\d+)?)\s+acres?",
            r"(?:containing|being|consisting of|contain)\s+([\d,]+(?:\.\d+)?)\s+acres?",
            r"([\d,]+(?:\.\d+)?)\s+acres?\s*(?:more or less|m\.o\.l\.)",
            r"tract\s+(?:no\.?\s*)?\d+[^,]+?([\d,]+(?:\.\d+)?)\s+acres?",
            r"parcel\s+(?:no\.?\s*)?\d+[^,]+?([\d,]+(?:\.\d+)?)\s+acres?",
        ]
        for pat in patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                try:
                    val = float(m.group(1).replace(",", ""))
                    if 0.1 < val < 10000:
                        return val
                except ValueError:
                    continue
        return None

    def _extract_session(self, url: str) -> Optional[str]:
        """Extract ASP.NET session ID from URL."""
        m = re.search(r"/\(S\((\w+)\)\)/", url)
        return m.group(1) if m else None

    def _solve_captcha(self, page_url: str, site_key: str) -> Optional[str]:
        """Solve reCAPTCHA v2 via 2captcha API."""
        import requests as http_req
        print("(solving captcha ...", end=" ", flush=True)
        try:
            resp = http_req.post(
                "https://2captcha.com/in.php", timeout=30,
                data={
                    "key": config.TWO_CAPTCHA_API_KEY,
                    "method": "userrecaptcha",
                    "googlekey": site_key,
                    "pageurl": page_url,
                    "json": 1,
                },
            )
            data = resp.json()
            if data.get("status") != 1:
                print(f"fail: {data.get('request', '?')})", end=" ", flush=True)
                return None
            rid = data["request"]
            for _ in range(30):
                time.sleep(5)
                resp = http_req.get(
                    "https://2captcha.com/res.php", timeout=30,
                    params={
                        "key": config.TWO_CAPTCHA_API_KEY,
                        "action": "get",
                        "id": rid,
                        "json": 1,
                    },
                )
                data = resp.json()
                if data.get("status") == 1:
                    print("solved)", end=" ", flush=True)
                    return data["request"]
        except Exception as e:
            print(f"error: {e})", end=" ", flush=True)
        return None

    def _inject_token_and_submit(self, page, token: str) -> None:
        """Set g-recaptcha-response and submit via __doPostBack."""
        page.evaluate("""(token) => {
            const ta = document.getElementById('g-recaptcha-response');
            if (ta) { ta.value = token; ta.textContent = token; }
            if (typeof ___grecaptcha_cfg !== 'undefined') {
                for (const cid in ___grecaptcha_cfg.clients) {
                    try {
                        const c = ___grecaptcha_cfg.clients[cid];
                        if (c && typeof c.callback === 'function') c.callback(token);
                    } catch(e) {}
                }
            }
        }""", token)
        page.wait_for_timeout(500)
        page.evaluate("""() => {
            window.__doPostBack(
                'ctl00$ContentPlaceHolder1$PublicNoticeDetailsBody1$btnViewNotice',
                ''
            );
        }""")

    @staticmethod
    def _find_chromium() -> Optional[str]:
        """Find chromium executable at runtime."""
        import glob
        import os
        import shutil
        candidates = [
            "~/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
            "~/.cache/ms-playwright/chromium-1226/chrome-linux64/chrome",
            "/opt/opencode/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            shutil.which("chromium"),
        ]
        for c in candidates:
            c = c.strip()
            if "*" in c:
                matches = glob.glob(c)
                if matches:
                    return sorted(matches)[-1]
            elif c and os.path.isfile(c):
                return c
        return None
