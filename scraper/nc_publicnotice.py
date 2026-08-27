"""NC Public Notice foreclosure scraper — ncnotices.com via Camoufox + 2captcha.

ncnotices.com is the same "Public Notice" ASP.NET WebForms platform as
tnpublicnotice.com / georgiapublicnotice.com (shared base in
:mod:`scraper.publicnotice_base`), but NC is reached through an authenticated
Smart Search account rather than a public popular-search category. We run a
pre-configured saved search (the 21 NC mountain counties) and, for each
result, solve the Cloudflare Turnstile gate and download the notice PDF (the
on-page HTML is a truncated OCR conversion; the PDF is canonical). Missing
acreage is enriched from NC OneMap via the parcel number.
"""
from __future__ import annotations
import logging
import random
import re
import time
from typing import Optional

from .base import PropertyData
from .config import (
    config,
    NCFORECLOSURES_BASE_URL,
    NC_MOUNTAIN_COUNTIES,
    NCNOTICES_EMAIL,
    NCNOTICES_PASSWORD,
    NCNOTICES_SAVED_SEARCH_ID,
    NCNOTICES_SAVED_SEARCH_NAME,
    NCNOTICES_SEARCH_KEYWORDS,
    NCNOTICES_SEARCH_TYPE,
    NCNOTICES_TURNSTILE_SITE_KEY,
)
from .publicnotice_base import (
    PublicNoticeScraper,
    trim_notice_body,
)

logger = logging.getLogger(__name__)

COUNTY_SET = set(NC_MOUNTAIN_COUNTIES)

# --- selectors (NC Smart-Search login / saved-search workflow) ---------------
GRID_ID = "ctl00_ContentPlaceHolder1_WSExtendedGrid1_GridView1"
SAVED_SEARCH_SELECT = 'select[name="ctl00$ContentPlaceHolder1$as1$ddlSavedSearches"]'
LOGIN_EMAIL = 'input[name="ctl00$ContentPlaceHolder1$AuthenticateIPA1$txtEmailAddress"]'
LOGIN_PASSWORD = 'input[name="ctl00$ContentPlaceHolder1$AuthenticateIPA1$txtPassword"]'
LOGIN_BTN = 'input[name="ctl00$ContentPlaceHolder1$AuthenticateIPA1$btnAuth"]'
COUNTY_LABEL_ATTR = "ctl00_ContentPlaceHolder1_as1_lstCounty_"

# --- notice-text parsing (NC-specific) --------------------------------------
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December"
)
_DATE_CORE = r"(?:" + _MONTHS + r")\s+\d{1,2},\s*\d{4}"
_SALE_DATE_RE = re.compile(
    r"\bon(?=[:\s])[:\s]*\s*\n?\s*(" + _DATE_CORE +
    r"(?:\s+AT\s+\d{1,2}:\d{2}(?:\s*[AP]\.?M\.?)?)?)",
    re.IGNORECASE,
)
_CASE_RE = re.compile(r"\b(\d{2}\s?(?:SP|CV|GS|ME)\d{5,9}(?:-\d+)?)\b")
_PIN_RE = re.compile(
    r"(?:parcel(?:\s*(?:id|no\.?|#))?|tax\s*(?:id|parcel)|p\.?i\.?n\.?)"
    r"\s*[:#]?\s*([0-9][0-9A-Za-z\-]{3,19})",
    re.IGNORECASE,
)
_COUNTY_OF_RE = re.compile(r"\bCOUNTY\s+OF\s+([A-Z][A-Za-z]+)\b")
_COUNTY_NAME_RE = re.compile(r"\b([A-Z][A-Za-z]+)\s+COUNTY\b")
_NC_HEADER_RE = re.compile(
    r"NORTH\s+CAROLINA[,\s:]+([A-Z][A-Za-z]+)\s+COUNTY\b", re.IGNORECASE
)
_ADDR_RE = re.compile(
    r"(?:street\s+)?address\s*:\s*(.{5,80}?(?:NC|,\s*\d{5}|\d{5}))", re.IGNORECASE
)
_COUNTY_STOPWORDS = {
    "registry", "register", "clerk", "superior", "court", "deed",
    "judicial", "superiorcourt", "chancery", "orphan",
}

# ncnotices.com's saved search returns a broad mix. We keep ONLY genuine *tax*
# foreclosures (positive tax-sale language); mortgage/deed-of-trust and
# HOA/assessment liens are excluded.
_MORTGAGE_RE = re.compile(
    r"deed\s+of\s+trust|deed\s+oftrust|substitute\s+trustee|"
    r"trustee'?s?\s+sale|power\s+of\s+sale|foreclosure\s+of\s+a\s+deed|"
    r"notice\s+of\s+trustee'?s?\s+sale",
    re.IGNORECASE,
)
_TAX_RE = re.compile(
    r"foreclosure\s+sale\s+to\s+satisfy\s+unpaid|"
    r"satisfy\s+unpaid\s+(?:property\s+)?taxes|"
    r"unpaid\s+property\s+taxes\s+owing|"
    r"taxes\s+owing\s+to|"
    r"\btax\s+foreclosure\b|"
    r"foreclosure\s+(?:of|for)\s+(?:the\s+)?tax|"
    r"tax\s+lien|lien\s+(?:for|of)\s+tax|"
    r"in\s+rem\s+foreclosure|"
    r"delinquent\s+tax|"
    r"commissioner\s+of\s+(?:revenue|taxes)|"
    r"chapter\s+105|"
    r"tax\s+sale|"
    r"certificate\s+of\s+tax|"
    r"delinquent\s+ad\s+valorem",
    re.IGNORECASE,
)


def _row_county(text: str) -> Optional[str]:
    """Extract the publication-county token from a results-grid row."""
    m = re.search(r"county:\s*([A-Za-z]+)", text, re.IGNORECASE)
    return m.group(1).lower() if m else None


def _find_auction_date(text: str) -> Optional[str]:
    """Find the sale/auction date in notice text."""
    m = _SALE_DATE_RE.search(text)
    return m.group(1).strip() if m else None


def _extract_county(text: str, all_counties: set[str]) -> Optional[str]:
    """Return the property county (lowercase) named in the notice header."""
    head = text[:1200]
    for regex in (_NC_HEADER_RE, _COUNTY_OF_RE):
        for m in regex.finditer(head):
            name = m.group(1).strip().lower()
            if name in all_counties:
                return name
    for m in _COUNTY_NAME_RE.finditer(head):
        name = m.group(1).strip().lower()
        if name in _COUNTY_STOPWORDS:
            continue
        if name in all_counties:
            return name
    return None


def _extract_address(text: str) -> Optional[str]:
    m = _ADDR_RE.search(text)
    if m:
        addr = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
        return addr or None
    return None


def _extract_pin(text: str) -> Optional[str]:
    for m in _PIN_RE.finditer(text):
        return m.group(1)
    return None


def _is_tax_foreclosure(notice: str) -> bool:
    """Return True only for genuine tax-foreclosure sale notices (NC)."""
    if not _TAX_RE.search(notice or ""):
        return False
    if _MORTGAGE_RE.search(notice or ""):
        return False
    return True


class NCPublicNoticeScraper(PublicNoticeScraper):
    """Scrape ncnotices.com tax/foreclosure notices for the 21 NC mountain counties."""

    SOURCE_NAME = "nc_publicnotice"
    BASE_URL = NCFORECLOSURES_BASE_URL
    TURNSTILE_SITE_KEY = NCNOTICES_TURNSTILE_SITE_KEY

    def __init__(self, keywords: str = NCNOTICES_SEARCH_KEYWORDS,
                 search_type: str = NCNOTICES_SEARCH_TYPE,
                 max_candidates: int = 600,
                 saved_search_id: str = NCNOTICES_SAVED_SEARCH_ID,
                 saved_search_name: str = NCNOTICES_SAVED_SEARCH_NAME):
        super().__init__(search_type="foreclosure", delay=1.5,
                         use_proxy=False, solve_captcha=True)
        self.keywords = keywords
        self.search_type = (search_type or "AND").upper()
        self.max_candidates = max_candidates
        self.saved_search_id = saved_search_id
        self.saved_search_name = (saved_search_name or "").strip()
        self._all_counties: set[str] = set()

    def _get_target_counties(self) -> set[str]:
        return COUNTY_SET

    # ------------------------------------------------------------------
    # browser helpers
    # ------------------------------------------------------------------

    def _login(self, page) -> bool:
        """Log in to ncnotices.com; True when the Smart Search area is reached."""
        for attempt in (1, 2):
            try:
                page.goto(self.BASE_URL + "/authenticate.aspx",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                page.fill(LOGIN_EMAIL, NCNOTICES_EMAIL)
                page.fill(LOGIN_PASSWORD, NCNOTICES_PASSWORD)
                page.click(LOGIN_BTN)
                page.wait_for_load_state("networkidle", timeout=60000)
                page.wait_for_timeout(3000)
                if "Smartsearch" in page.url:
                    print(f"  logged in (session {self._extract_session(page.url)})")
                    return True
                body = page.evaluate("() => document.body.innerText || ''")
                logger.warning("login attempt %d did not land on Smartsearch: %s",
                               attempt, body[:200].replace("\n", " "))
            except Exception as e:
                logger.warning("login attempt %d failed: %s", attempt, e)
            time.sleep(3 * attempt)
        return False

    def _session_url(self, page, page_name: str) -> str:
        sid = self._extract_session(page.url)
        return f"{self.BASE_URL}/(S({sid}))/Smartsearch/{page_name}"

    # ------------------------------------------------------------------
    # keyword search (saved search)
    # ------------------------------------------------------------------

    def _run_keyword_search(self, page) -> bool:
        """Run the pre-configured saved search (the reliable, proven path).

        ncnotices.com's manual keyword form (Go button + county checkboxes) is
        driven by server-side UpdatePanel postbacks that Camoufox cannot
        reliably drive, so we instead select a saved search (configured on the
        site with the desired 21-county / keyword / match-type criteria).
        """
        page.goto(self._session_url(page, "Default.aspx"),
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(SAVED_SEARCH_SELECT, timeout=30000)
        page.wait_for_timeout(1500)

        # Capture the site's full county list for validation.
        self._all_counties = set(page.evaluate(
            "prefix => Array.from(document.querySelectorAll("
            "'label[for^=\"' + prefix + '\"]'))"
            "  .map(l => (l.textContent || '').trim().toLowerCase())"
            "  .filter(Boolean)",
            COUNTY_LABEL_ATTR))
        if not self._all_counties:
            logger.warning("could not read county list from search form")

        opts = page.evaluate(
            "(sel) => { const e = document.querySelector(sel);"
            " if (!e) return [];"
            " return Array.from(e.options)"
            ".map(o => ({v: o.value, t: (o.textContent || '').trim()})); }",
            SAVED_SEARCH_SELECT)
        chosen = None
        if self.saved_search_id:
            chosen = next((o for o in opts if o["v"] == self.saved_search_id), None)
        if not chosen and self.saved_search_name:
            ln = self.saved_search_name.lower()
            chosen = next((o for o in opts if ln in o["t"].lower()), None)
        if not chosen:
            for kw in ("mountain", "foreclosure"):
                chosen = next((o for o in opts if kw in o["t"].lower()), None)
                if chosen:
                    break
        if not chosen:
            logger.error("no usable saved search found; options: %s",
                         [o["t"] for o in opts])
            return False
        print(f"  saved search: '{chosen['t']}' (id {chosen['v']})")

        page.select_option(SAVED_SEARCH_SELECT, chosen["v"])
        try:
            page.wait_for_load_state("networkidle", timeout=60000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        try:
            page.wait_for_selector(f"#{GRID_ID}", timeout=60000)
        except Exception:
            logger.error("search results grid did not appear after saved-search selection")
            return False
        return True

    def _grid_state(self, page) -> dict:
        return page.evaluate("""(grid) => {
            const t = {};
            const total = document.getElementById(grid + '_ctl01_lblTotalPages');
            const cur = document.getElementById(grid + '_ctl01_lblCurrentPage');
            t.total = total ? total.textContent.trim() : null;
            t.current = cur ? cur.textContent.trim() : null;
            const next = document.getElementById(grid + '_ctl01_btnNext');
            t.next_disabled = next ? next.disabled : true;
            const rows = [];
            document.querySelectorAll('tr').forEach(r => {
                const hdn = r.querySelector('input[id*="hdnPKValue"]');
                if (!hdn) return;
                const btn = r.querySelector('input[id*="btnView"]');
                const m = btn ? (btn.getAttribute('onclick') || '').match(/ID=(\\d+)/) : null;
                rows.push({
                    pk: hdn.value,
                    id: m ? m[1] : null,
                    text: (r.textContent || '').replace(/\\s+/g, ' ').trim(),
                });
            });
            t.rows = rows;
            return t;
        }""", GRID_ID)

    def _page_count(self, state: dict) -> int:
        m = re.search(r"(\d+)", state.get("total") or "")
        return int(m.group(1)) if m else 1

    # ------------------------------------------------------------------
    # detail extraction
    # ------------------------------------------------------------------

    def _extract_detail(self, page, cand: dict) -> Optional[dict]:
        """Open a detail page and return {'text': ..., 'had_pdf': bool} or None."""
        sid = self._extract_session(page.url)
        url = f"{self.BASE_URL}/(S({sid}))/Details.aspx?SID={sid}&ID={cand['id']}"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        if not self._pass_turnstile_gate(page, self.TURNSTILE_SITE_KEY):
            logger.warning("detail content unavailable for %s", cand["id"])
            return None

        notice = page.evaluate("() => document.body.innerText || ''")
        pdf_text = self._fetch_pdf_text(page, cand["id"])
        if pdf_text and len(pdf_text) > 300:
            return {"text": trim_notice_body(pdf_text), "had_pdf": True, "url": url}

        notice = trim_notice_body(notice)
        if len(notice) < 200:
            logger.warning("notice text too short (%d) for %s",
                           len(notice), cand["id"])
            return None
        return {"text": notice, "had_pdf": False, "url": url}

    def _build_property(self, cand: dict, detail: dict) -> Optional[PropertyData]:
        notice = detail["text"]
        county = _extract_county(notice, self._all_counties)
        if county not in COUNTY_SET:
            return None

        if not _is_tax_foreclosure(notice):
            logger.info("Skipping non-tax foreclosure (mortgage/HOA/other lien) for %s",
                        cand.get("id"))
            return None

        acres = self._extract_acreage(notice)
        case_m = _CASE_RE.search(notice)
        auction_date = _find_auction_date(notice)
        pin = _extract_pin(notice)
        address = _extract_address(notice)

        return PropertyData(
            source=self.SOURCE_NAME,
            source_listing_id=cand["id"],
            url=detail["url"],
            address=address,
            city=None,
            county=county,
            state="NC",
            zip_code=None,
            latitude=None,
            longitude=None,
            price=1,
            acres=acres,
            description=notice[:2000],
            property_type="foreclosure",
            court_case=case_m.group(1).replace(" ", "") if case_m else None,
            auction_date=auction_date,
            parcel_number=pin,
            raw_source_text=notice,
        )

    # ------------------------------------------------------------------
    # GIS acreage enrichment (in-memory, before acreage filter)
    # ------------------------------------------------------------------

    def _enrich_acres(self, props: list[PropertyData]) -> list[PropertyData]:
        """Fill missing acreage (and coords/address) from NC OneMap via PIN."""
        from .nc_gis_lookup import NC1MapService
        svc = NC1MapService()
        enriched = 0
        for p in props:
            if p.get("acres") is not None or not p.get("parcel_number"):
                continue
            try:
                data = svc.by_parcel(p["parcel_number"], county=p.get("county"))
            except Exception as e:
                logger.warning("GIS lookup failed for %s: %s", p["parcel_number"], e)
                data = None
            if data and data.get("acres"):
                p["acres"] = data["acres"]
                p["acres_source"] = "gis"
                if data.get("latitude"):
                    p["latitude"] = data["latitude"]
                if data.get("longitude"):
                    p["longitude"] = data["longitude"]
                if not p.get("address") and data.get("siteadd"):
                    p["address"] = data["siteadd"]
                if not p.get("parcel_number") and data.get("parno"):
                    p["parcel_number"] = data["parno"]
                enriched += 1
            time.sleep(0.6)
        print(f"  GIS acreage enrichment: {enriched} of {len(props)} filled")
        return props

    # ------------------------------------------------------------------
    # main flow
    # ------------------------------------------------------------------

    def scrape(self) -> list[PropertyData]:
        print(f"\n  NC PUBLIC NOTICE FORECLOSURES (ncnotices.com keyword search)")
        print(f"  keywords: '{self.keywords}' ({self.search_type})")
        print(f"  Target counties: {len(COUNTY_SET)}")
        if not NCNOTICES_EMAIL or not NCNOTICES_PASSWORD:
            logger.error("NCNOTICES_EMAIL / NCNOTICES_PASSWORD not set")
            return []

        properties: list[PropertyData] = []
        from camoufox.sync_api import Camoufox
        with Camoufox(headless="virtual", humanize=False) as browser:
            page = browser.new_page()
            page.set_viewport_size({"width": 1600, "height": 1000})
            page.set_default_timeout(45000)

            if not self._login(page):
                return []
            if not self._run_keyword_search(page):
                return []

            state = self._grid_state(page)
            print(f"  Results: {state.get('total') or '?'} — "
                  f"{len(state.get('rows', []))} rows on page 1")

            candidates: list[dict] = []
            seen: set[str] = set()
            page_num = 1
            while True:
                for row in state.get("rows", []):
                    rid = row.get("id") or row.get("pk")
                    if not rid or rid in seen:
                        continue
                    rc = _row_county(row.get("text", ""))
                    if rc and rc not in COUNTY_SET:
                        continue
                    seen.add(rid)
                    candidates.append(row)
                if not candidates or len(candidates) >= self.max_candidates:
                    break
                if state.get("next_disabled"):
                    break
                if page_num >= self._page_count(state):
                    break
                next_label = state.get("current")
                page.click(f"#{GRID_ID}_ctl01_btnNext")
                try:
                    page.wait_for_load_state("networkidle", timeout=60000)
                except Exception:
                    pass
                for _ in range(30):
                    page.wait_for_timeout(1000)
                    state = self._grid_state(page)
                    if state.get("current") != next_label:
                        break
                page_num += 1
                print(f"  page {page_num}: {len(state.get('rows', []))} rows, "
                      f"{len(candidates)} collected so far")
                if not state.get("rows"):
                    break

            candidates = candidates[:self.max_candidates]
            print(f"  Candidates to process: {len(candidates)}")

            for i, cand in enumerate(candidates, 1):
                print(f"  [{i}/{len(candidates)}] {cand['id']}", end=" ", flush=True)
                time.sleep(random.uniform(1.0, 2.0))
                try:
                    detail = self._extract_detail(page, cand)
                except Exception as e:
                    logger.warning("detail error for %s: %s", cand["id"], e)
                    detail = None
                if not detail:
                    print("(skipped)")
                    continue
                prop = self._build_property(cand, detail)
                if prop:
                    print(f"-> {prop['county']} "
                          f"{prop.get('acres') if prop.get('acres') is not None else '?'}ac "
                          f"{'pdf' if detail['had_pdf'] else 'html'}")
                    properties.append(prop)
                else:
                    print("(non-target county)")

            page.close()

        return properties

    def run(self) -> list[PropertyData]:
        """Scrape, GIS-enrich acreage, then filter by county + MIN_ACRES."""
        try:
            props = self.scrape()
        except Exception as e:
            logger.error("Scraper %s failed: %s", self.SOURCE_NAME, e, exc_info=True)
            return []
        print(f"  Scrape: {len(props)} target-county properties")

        props = self._enrich_acres(props)
        return self._apply_county_acreage_filter(props)


def scrape_all() -> list[PropertyData]:
    return NCPublicNoticeScraper().run()
