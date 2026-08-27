"""NC Foreclosure Notices scraper — ncnotices.com via Camoufox + 2captcha.

Uses an authenticated Smart Search account and runs a keyword search
(foreclosure / tax / unpaid, "All Words") scoped to the 21 NC mountain
counties. For each result it opens the detail page, solves the
Cloudflare Turnstile gate via 2captcha, and downloads the notice PDF
when one exists (the on-page HTML text is an OCR conversion of that PDF
and is truncated; the PDF is the canonical full notice). Acreage missing
from the notice text is enriched from NC OneMap GIS via the parcel number.
"""
from __future__ import annotations
import html as html_lib
import io
import json
import logging
import random
import re
import time
from typing import Optional
from urllib.parse import urljoin

from .base import BaseForeclosureScraper, PropertyData
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

logger = logging.getLogger(__name__)

COUNTY_SET = set(NC_MOUNTAIN_COUNTIES)

# --- selectors -----------------------------------------------------------
GRID_ID = "ctl00_ContentPlaceHolder1_WSExtendedGrid1_GridView1"
SAVED_SEARCH_SELECT = 'select[name="ctl00$ContentPlaceHolder1$as1$ddlSavedSearches"]'
LOGIN_EMAIL = 'input[name="ctl00$ContentPlaceHolder1$AuthenticateIPA1$txtEmailAddress"]'
LOGIN_PASSWORD = 'input[name="ctl00$ContentPlaceHolder1$AuthenticateIPA1$txtPassword"]'
LOGIN_BTN = 'input[name="ctl00$ContentPlaceHolder1$AuthenticateIPA1$btnAuth"]'
SEARCH_INPUT = 'input[name="ctl00$ContentPlaceHolder1$as1$txtSearch"]'
RADIO_ALL_WORDS = "input#ctl00_ContentPlaceHolder1_as1_rdoType_0"   # All Words (AND)
RADIO_ANY_WORDS = "input#ctl00_ContentPlaceHolder1_as1_rdoType_1"   # Any Words (OR)
GO_BTN = 'input[name="ctl00$ContentPlaceHolder1$as1$btnGo1"]'
GO_BTN_ALT = 'input[name="ctl00$ContentPlaceHolder1$as1$btnGo"]'
COUNTY_LABEL_ATTR = "ctl00_ContentPlaceHolder1_as1_lstCounty_"
TURNSTILE_FIELD = 'input[name="cf-turnstile-response"]'
VIEW_NOTICE_BTN = 'input[name="ctl00$ContentPlaceHolder1$PublicNoticeDetailsBody1$btnViewNotice"]'
DOWNLOAD_LINK = 'a[id*="lnkDownload"]'
MESSAGE_LABEL = "ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_lblMessage"

# --- notice-text parsing -------------------------------------------------
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
# Words that follow a county name but are not the county itself
_COUNTY_STOPWORDS = {
    "registry", "register", "clerk", "superior", "court", "deed",
    "judicial", "superiorcourt", "chancery", "orphan",
}


def _row_county(text: str) -> Optional[str]:
    """Extract the publication-county token from a results-grid row."""
    m = re.search(r"county:\s*([A-Za-z]+)", text, re.IGNORECASE)
    return m.group(1).lower() if m else None


def _find_auction_date(text: str) -> Optional[str]:
    """Find the sale/auction date in notice text."""
    m = _SALE_DATE_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _extract_county(text: str, all_counties: set[str]) -> Optional[str]:
    """Return the property county (lowercase) named in the notice header.

    Prefers 'COUNTY OF X' and 'NORTH CAROLINA, X COUNTY'; falls back to
    any '<NAME> COUNTY' token. Only names in ``all_counties`` (the full
    NC county list from the site) are accepted.
    """
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


# ncnotices.com's saved search returns a broad mix: tax foreclosures, mortgage
# (deed-of-trust / trustee) foreclosures, HOA/assessment lien foreclosures, and
# procedural "notice of service by process by publication" postings. We keep ONLY
# genuine *tax* foreclosures, identified by positive tax-sale language. The detail
# text often bundles a "service by publication" preamble with the real tax-sale
# body, so we must match the tax language rather than naively excluding anything
# that mentions service by publication.
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


def _is_tax_foreclosure(notice: str) -> bool:
    """Return True only for genuine tax-foreclosure sale notices.

    Requires positive tax-sale language (e.g. "foreclosure sale to satisfy
    unpaid property taxes", "tax foreclosure", "tax lien", "in rem", NCGS
    Chapter 105). Mortgage/deed-of-trust foreclosures and HOA/assessment liens
    contain no such language, so they are excluded.
    """
    if not _TAX_RE.search(notice or ""):
        return False
    if _MORTGAGE_RE.search(notice or ""):
        return False
    return True


def _extract_pdf_text(data: bytes) -> Optional[str]:
    """Extract text from PDF bytes via pdfplumber."""
    if not data or data[:4] != b"%PDF":
        return None
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed; cannot parse PDF")
        return None
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages).strip()
        return text or None
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return None


class NCForeclosureScraper(BaseForeclosureScraper):
    """Scrape ncnotices.com tax/foreclosure notices for the 21 NC mountain counties."""

    SOURCE_NAME = "ncforeclosures"
    BASE_URL = NCFORECLOSURES_BASE_URL

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
    # keyword search
    # ------------------------------------------------------------------

    def _run_keyword_search(self, page) -> bool:
        """Run a pre-configured saved search (the reliable, proven path).

        ncnotices.com's manual keyword form (Go button + county checkboxes)
        is driven by server-side UpdatePanel postbacks that Camoufox cannot
        reliably drive, so we instead select a saved search (configured on
        the site with the desired 21-county / keyword / match-type criteria).
        Selecting it posts back and lands on the Search.aspx results grid.
        Also captures the site's full county list for later validation.
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

        # Pick the saved search: explicit id first, then name substring,
        # then a sensible fallback on "mountain"/"foreclosure".
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

    def _pass_detail_gate(self, page, notice_id: str) -> bool:
        """Solve the Turnstile gate (if present) and reveal the notice body.

        Returns True when the full notice content is visible.
        """
        # Wait for either a Turnstile widget or the content itself.
        for _ in range(10):
            if page.query_selector(TURNSTILE_FIELD):
                break
            if self._detail_visible(page, notice_id):
                return True
            page.wait_for_timeout(1500)

        if not page.query_selector(TURNSTILE_FIELD):
            return self._detail_visible(page, notice_id)

        token = self._solve_turnstile(page.url, NCNOTICES_TURNSTILE_SITE_KEY)
        if not token:
            logger.warning("turnstile solve failed for %s", notice_id)
            return False
        self._inject_turnstile_token(page, token)
        page.wait_for_timeout(800)
        try:
            page.click(VIEW_NOTICE_BTN, timeout=20000)
        except Exception as e:
            logger.warning("btnViewNotice click failed for %s: %s", notice_id, e)
            return False
        try:
            page.wait_for_load_state("load", timeout=60000)
        except Exception:
            pass
        for _ in range(24):
            if self._detail_visible(page, notice_id):
                return True
            page.wait_for_timeout(2500)
        return self._detail_visible(page, notice_id)

    def _detail_visible(self, page, notice_id: str) -> bool:
        body_len = page.evaluate("() => (document.body.innerText || '').length")
        msg = page.evaluate(
            "() => { const el = document.getElementById('%s');"
            " return el ? (el.textContent || '').trim() : ''; }" % MESSAGE_LABEL)
        if "complete the challenge" in (msg or "").lower():
            return False
        return body_len > 1500

    def _fetch_pdf_text(self, page, notice_id: str) -> Optional[str]:
        """Download and parse the notice PDF if a download link exists.

        Returns the extracted PDF text. The on-page HTML notice is an OCR
        conversion that gets truncated, so the PDF text is used as the
        canonical (full) source text whenever a PDF is available.
        """
        el = page.query_selector(DOWNLOAD_LINK)
        if not el:
            return None
        try:
            href = el.get_attribute("href")
        except Exception:
            return None
        if not href:
            return None
        href = html_lib.unescape(href)
        # The download link is a server-relative URL (e.g.
        # "PDFDocument.aspx?SID=...&FileName=..."). page.request.get() does
        # not reliably resolve relative URLs against the page origin, so
        # resolve it explicitly against the current page URL — otherwise the
        # request fails with "Invalid URL" and we silently fall back to the
        # truncated on-page HTML (losing the full PDF text and any PIN).
        abs_href = urljoin(page.url, href)
        try:
            resp = page.request.get(abs_href, timeout=60000)
            data = resp.body()
        except Exception as e:
            logger.warning("PDF download failed for %s: %s", notice_id, e)
            return None
        text = _extract_pdf_text(data)
        if text:
            logger.debug("PDF for %s: %d chars", notice_id, len(text))
        return text

    def _extract_detail(self, page, cand: dict) -> Optional[dict]:
        """Open a detail page and return {'text': ..., 'had_pdf': bool} or None."""
        sid = self._extract_session(page.url)
        url = f"{self.BASE_URL}/(S({sid}))/Details.aspx?SID={sid}&ID={cand['id']}"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        if not self._pass_detail_gate(page, cand["id"]):
            logger.warning("detail content unavailable for %s", cand["id"])
            return None

        pdf_text = self._fetch_pdf_text(page, cand["id"])
        if pdf_text and len(pdf_text) > 300:
            return {"text": pdf_text, "had_pdf": True, "url": url}

        body_text = page.evaluate("() => document.body.innerText || ''")
        notice = self._parse_html_notice(body_text)
        if len(notice) < 200:
            logger.warning("notice text too short (%d) for %s",
                           len(notice), cand["id"])
            return None
        return {"text": notice, "had_pdf": False, "url": url}

    def _parse_html_notice(self, body_text: str) -> str:
        """Return the HTML notice body (text after the 'Notice Content' marker)."""
        if "Notice Content" in body_text:
            body_text = body_text.split("Notice Content", 1)[1]
        for marker in ("Powered by Translate", "Copyright ©"):
            i = body_text.find(marker)
            if i > 0:
                body_text = body_text[:i]
        return body_text.strip()

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
        print(f"\n  NC FORECLOSURES (ncnotices.com keyword search)")
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
                    # Cheap pre-filter on the grid's publication-county column
                    # (local foreclosures are almost always published in-county).
                    # The real property county is re-validated from the notice
                    # body in _build_property, so this only trims obvious
                    # non-target rows before we spend a Turnstile solve.
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

        state_counties = self._get_target_counties()
        filtered = []
        skipped = 0
        for prop in props:
            county = (prop.get("county") or "").lower().strip()
            acres = prop.get("acres")
            if county in state_counties and (acres is None or acres >= config.MIN_ACRES):
                prop["county"] = county.title()
                filtered.append(prop)
            else:
                skipped += 1
        print(f"  After filtering: {len(filtered)} qualifying, {skipped} skipped")
        return filtered


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def scrape_all() -> list[PropertyData]:
    return NCForeclosureScraper().run()
