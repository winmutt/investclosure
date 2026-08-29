"""Shared base for the "Public Notice" ASP.NET WebForms foreclosure scrapers.

``nc_publicnotice`` (ncnotices.com), ``tn_publicnotice`` (tnpublicnotice.com)
and ``ga_publicnotice`` (georgiapublicnotice.com) are all the same
Legacy/PublicNotice ASP.NET WebForms platform:

* an identical GridView of results keyed by ``hdnPKValue`` hidden inputs,
* a Cloudflare Turnstile + "View Notice" (``__doPostBack``) detail gate,
* a truncated on-page OCR notice with a downloadable full-text PDF,
* the same tax-vs-mortgage foreclosure classifier patterns.

This module factors that common behaviour into :class:`PublicNoticeScraper` so
the three per-state modules stay thin and consistent.
"""
from __future__ import annotations
import html as html_lib
import io
import logging
import re
from datetime import date, datetime, timedelta
from typing import List, Optional

from .base import BaseForeclosureScraper, PropertyData

logger = logging.getLogger(__name__)

# --- shared search recency --------------------------------------------------
# Limit the grid search to notices *published* within the last N days. The
# sites paginate newest-first, so this keeps each run scoped to recent
# publications instead of re-processing months of stale notices.
LOOKBACK_DAYS = 7

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# "Published: 8/21/2026" / "Posted: 8/21/2026" tokens (NC grid rows).
_PUBLISHED_TOKEN_RE = re.compile(
    r"Published:\s*(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE)

# Leading full date in the notice body, e.g. "Tuesday, August 25, 2026"
# (TN/GA grid full_text starts with the publication date).
_FULL_DATE_RE = re.compile(
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*)?"
    r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})"
)


def _parse_grid_publication_date(text: str) -> Optional[date]:
    """Best-effort publication date from a PublicNotice grid row's text.

    Tries the explicit ``Published: M/D/YYYY`` token first (NC), then the
    leading calendar date embedded in the notice body (TN/GA). Returns
    ``None`` when no date can be parsed (callers keep such rows).
    """
    if not text:
        return None
    m = _PUBLISHED_TOKEN_RE.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    m = _FULL_DATE_RE.search(text)
    if m:
        mon = _MONTHS.get(m.group(1).title())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                pass
    return None


def _is_recent_publication(text: str, days: int = LOOKBACK_DAYS,
                           today: Optional[date] = None) -> bool:
    """True when the grid row's publication date is within ``days`` of today.

    Rows with no parseable date return True (do not drop unparseable rows).
    """
    d = _parse_grid_publication_date(text)
    if d is None:
        return True
    today = today or date.today()
    return d >= today - timedelta(days=days)

# --- shared selectors --------------------------------------------------------
TURNSTILE_FIELD = 'input[name="cf-turnstile-response"]'
VIEW_NOTICE_BTN = 'input[name="ctl00$ContentPlaceHolder1$PublicNoticeDetailsBody1$btnViewNotice"]'
DOWNLOAD_LINK = 'a[id*="lnkDownload"]'
PER_PAGE_SELECT = 'select[name*="ddlPerPage"]'

# --- shared PDF extraction ---------------------------------------------------
def extract_pdf_text(data: bytes) -> Optional[str]:
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


# --- shared tax / mortgage foreclosure classifier ----------------------------
# Strong signals that a notice is a MORTGAGE / DEED-OF-TRUST (bank) sale.
MORTGAGE_FC_PATTERNS = [
    r"deed\s+of\s+trust",
    r"substitute\s+trustee",
    r"owner\s+of\s+debt",
    r"\bbeneficiary\b",
    r"pursuant\s+to\s+(?:a\s+|the\s+)?deed\s+of\s+trust",
]
# Signals that a notice is a TAX foreclosure / tax sale (county trustee sale
# for delinquent property taxes, under T.C.A. ch. 67 / NCGS ch. 105 / etc.).
TAX_FC_PATTERNS = [
    r"tax\s+sale",
    r"tax\s+foreclosure",
    r"tax\s+lien",
    r"delinquent\s+tax",
    r"for\s+delinquent\s+taxes",
    r"sale\s+of\s+real\s+property\s+for",
    r"tennessee\s+code\s+annotated\s*[§]?\s*67",
    r"\bt\.?c\.?a\.?\s*[§]?\s*67",
    r"section\s+67[-\s]",
    r"county\s+trustee",
]

# Strong, authoritative tax-sale signals. A notice that ALSO reads as a
# mortgage/deed-of-trust (bank) sale must carry one of THESE to be accepted as
# a genuine tax foreclosure -- a bare "T.C.A. 67" reference or "delinquent tax"
# aside is not enough (e.g. a substitute-trustee deed-of-trust sale that merely
# mentions taxes in its default language).
STRONG_TAX_FC_PATTERNS = [
    r"county\s+trustee",
    r"delinquent\s+property\s+taxes",
    r"delinquent\s+tax\s+sale",
    r"tax\s+sale",
    r"for\s+delinquent\s+taxes",
    r"t\.?c\.?a\.?\s*[§]?\s*67-5",
    r"tennessee\s+code\s+annotated\s*[§]?\s*67-5",
    r"section\s+67-5",
]

# --- shared street-address extraction ---------------------------------------
# Common US street-address pattern used by TN / GA notice bodies.
ADDRESS_RE = re.compile(
    r"\b(\d{1,5}\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+){0,4}?\s*"
    r"(?:STREET|ST|AVENUE|AVE|BOULEVARD|BLVD|DRIVE|DR|ROAD|RD|LANE|LN|"
    r"HIGHWAY|HWY|COURT|CT|CIRCLE|CIR|PKWY|PIKE|WAY)\.?)",
    re.IGNORECASE,
)
# Court/tribunal names that must not be mistaken for a street address.
_COURT_STOPWORDS_RE = re.compile(
    r"\b(chancery|circuit|county|district|superior|federal|probate|"
    r"juvenile|municipal|supreme)\s+court\b",
    re.IGNORECASE,
)


def trim_notice_body(text: str) -> str:
    """Trim boilerplate around the actual notice body.

    Cuts the text down to what follows the ``Notice Content`` marker and stops
    at the ``Powered by Translate`` / ``Copyright`` footer so the extracted
    notice text is not polluted by site chrome.
    """
    if "Notice Content" in text:
        text = text.split("Notice Content", 1)[1]
    for marker in ("Powered by Translate", "Copyright ©"):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx]
    return text.strip()


def extract_street_address(text: str) -> Optional[str]:
    """Extract the first street address found in a notice body (shared)."""
    if not text:
        return None
    m = ADDRESS_RE.search(text)
    if not m:
        return None
    addr = m.group(1).strip()
    addr = re.sub(r"\s+", " ", addr)
    if _COURT_STOPWORDS_RE.search(addr):
        return None
    return addr[:120] if addr else None


def normalize_notice_text(text: str) -> str:
    """Re-insert word breaks into PDF-extracted notice text.

    pdfplumber sometimes yields text with all inter-word spaces stripped
    (e.g. "NOTICEOFSULLIVANCOUNTYDELINQUENTTAXSALE"), which breaks the
    whitespace-dependent tax/mortgage classifiers below. Re-insert a space at
    letter/digit/section-symbol boundaries so "DELINQUENTTAXSALE" becomes
    "DELINQUENT TAX SALE" and classification works regardless of extraction
    quality. Harmless on already-spaced text.
    """
    if not text:
        return text
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=§)", " ", text)
    return re.sub(r"\s+", " ", text)


def dedup_by_content(properties: List[PropertyData]) -> List[PropertyData]:
    """Collapse properties sharing the same (lowercased) notice text.

    Public-notice sites frequently re-list one consolidated notice under
    several grid-row ids; this keeps a single property per notice body so a
    single run never inserts N copies of one notice.
    """
    import hashlib
    seen: set[str] = set()
    deduped: List[PropertyData] = []
    for p in properties:
        text = (p.get("raw_source_text") or p.get("description") or "")
        if text:
            key = hashlib.md5(text.lower().encode("utf-8")).hexdigest()
        else:
            key = ("loc", p.get("county") or "", p.get("state") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    if len(deduped) != len(properties):
        logger.info("Within-run dedup: dropped %d duplicate notice(s)",
                    len(properties) - len(deduped))
    return deduped


class PublicNoticeScraper(BaseForeclosureScraper):
    """Base scraper for the shared "Public Notice" ASP.NET WebForms backend.

    Subclasses (NCPublicNoticeScraper / TNPublicNoticeScraper /
    GAPublicNoticeScraper) supply the state-specific config (BASE_URL,
    TURNSTILE_SITE_KEY, target counties, search strategy) and any extra
    classification or per-parcel parsing; everything else lives here.
    """

    SOURCE_NAME = "publicnotice"
    BASE_URL = ""
    TURNSTILE_SITE_KEY = ""

    # GridView unique-id for the Next button (used by __doPostBack).
    NEXT_BTN_UNIQUE_ID = "ctl00$ContentPlaceHolder1$WSExtendedGridNP1$btnNext"

    # ---- grid helpers ------------------------------------------------------

    def _grid_pks(self, page) -> List[str]:
        """Current GridView primary-key values (used to detect a refresh)."""
        try:
            return page.evaluate(
                "() => Array.from(document.querySelectorAll('input[id*=\"hdnPKValue\"]')).map(r => r.value)"
            )
        except Exception:
            return []

    def _wait_grid_refresh(self, page, old_pks, timeout: int = 30000) -> None:
        """Wait until the GridView rows change from ``old_pks``.

        The search/county postbacks are AJAX (UpdatePanel) updates, so waiting
        on ``load``/``networkidle`` or on the row marker selector is not enough
        -- the old rows are still present until the new response renders. We
        poll until the rendered PK set differs (or the grid goes empty).
        """
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass
        old = set(old_pks)
        for _ in range(int(timeout / 1000)):
            try:
                cur = set(self._grid_pks(page))
            except Exception:
                cur = old
            if cur != old:
                return
            page.wait_for_timeout(1000)

    def _page_info(self, page) -> Optional[dict]:
        """Return {'cur':int,'total':int} parsed from the GridView pager, or None."""
        return page.evaluate(
            """() => {
                const tds = Array.from(document.querySelectorAll('td'));
                const p = tds.map(td => (td.innerText||'').trim())
                             .find(t => /Page\\s+\\d+\\s+of\\s+\\d+/i.test(t));
                if (!p) return null;
                const m = p.match(/Page\\s+(\\d+)\\s+of\\s+(\\d+)/i);
                return m ? {cur: parseInt(m[1],10), total: parseInt(m[2],10)} : null;
            }"""
        )

    def _goto_next_page(self, page, next_num: int) -> bool:
        """Advance the GridView pager. Returns True if advanced.

        Prefers invoking the ASP.NET ``__doPostBack`` for the Next button's
        server-side event (the reliable path); falls back to clicking a pager
        link labelled with the page number or ``next``/``>``/``»``/``...``.
        """
        old_pks = self._grid_pks(page)
        btn_id = page.evaluate(
            """() => {
                const b = document.querySelector('input[id*="btnNext"]');
                return b ? (b.getAttribute('name') || b.name) : null;
            }"""
        )
        if btn_id:
            try:
                page.evaluate("(btnId) => __doPostBack(btnId, '')", btn_id)
            except Exception as e:
                logger.warning("__doPostBack(next) failed: %s", e)
                return False
        else:
            ok = page.evaluate(
                """(n) => {
                    const links = Array.from(document.querySelectorAll('td a, td span'));
                    for (const a of links) {
                        if ((a.innerText||'').trim() === String(n)) { a.click(); return true; }
                    }
                    for (const a of links) {
                        const t = (a.innerText||'').trim().toLowerCase();
                        if (t === 'next' || t === '>' || t === '»' || t === '...') { a.click(); return true; }
                    }
                    return false;
                }""",
                next_num,
            )
            if not ok:
                return False
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        try:
            page.wait_for_selector('input[id*="hdnPKValue"]', state="attached", timeout=20000)
        except Exception:
            pass
        self._wait_grid_refresh(page, old_pks)
        return True

    def _county_from_grid_text(self, full_text: str) -> Optional[str]:
        """Override in subclass to pull the property county from grid-row text."""
        return None

    def _parse_grid_records_once(self, page) -> List[dict]:
        """Parse ASP.NET GridView rows into record dicts (no retry)."""
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

                const spMatch = fullText.match(/\\b(\\d+SP\\d+[-\\w]*)\\b/);
                const spCase = spMatch ? spMatch[1] : null;

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
                    detail_url: detailUrl,
                    full_text: fullText,
                });
            });
            return results;
        }""")
        for r in records:
            r["county"] = self._county_from_grid_text(r.get("full_text") or "") or None
        return records

    def _parse_grid_records(self, page) -> List[dict]:
        """Parse the GridView, retrying on mid-navigation errors."""
        last_err = None
        for _ in range(4):
            try:
                return self._parse_grid_records_once(page)
            except Exception as e:  # page navigated mid-evaluate
                last_err = e
                page.wait_for_timeout(2500)
        logger.warning("Grid parse failed after retries: %s", last_err)
        return []

    # ---- detail extraction --------------------------------------------------

    def _notice_body_visible(self, page) -> bool:
        """True once the notice body has rendered past the Turnstile challenge."""
        body_len = page.evaluate(
            "() => (document.body ? (document.body.innerText||'').length : 0)"
        )
        msg = page.evaluate(
            "() => { const el = document.getElementById("
            "'ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_lblMessage');"
            " return el ? (el.textContent || '').toLowerCase() : ''; }"
        )
        if "complete the challenge" in (msg or ""):
            return False
        return body_len > 1500

    def _pass_turnstile_gate(self, page, site_key: str) -> bool:
        """Solve the Turnstile gate (if present) and reveal the notice body.

        Returns True when the full notice content is visible.
        """
        if self._notice_body_visible(page):
            return True
        turnstile = False
        for _ in range(10):
            if page.query_selector(TURNSTILE_FIELD):
                turnstile = True
                break
            if self._notice_body_visible(page):
                return True
            page.wait_for_timeout(1500)

        if not turnstile and not page.query_selector(TURNSTILE_FIELD):
            return self._notice_body_visible(page)

        if not self.solve_captcha:
            logger.warning("Turnstile present but captcha solving disabled")
            return False
        token = self._solve_turnstile(page.url, site_key)
        if not token:
            logger.warning("turnstile solve failed")
            return False
        self._inject_turnstile_token(page, token)
        page.wait_for_timeout(800)
        try:
            page.evaluate(
                "() => __doPostBack("
                "'ctl00$ContentPlaceHolder1$PublicNoticeDetailsBody1$btnViewNotice', '')"
            )
        except Exception as e:
            logger.warning("btnViewNotice submit failed: %s", e)
            return False
        try:
            page.wait_for_load_state("load", timeout=60000)
        except Exception:
            pass
        for _ in range(24):
            if self._notice_body_visible(page):
                return True
            page.wait_for_timeout(2500)
        return self._notice_body_visible(page)

    def _fetch_pdf_text(self, page, notice_id: str) -> Optional[str]:
        """Download and parse the notice PDF if a download link exists.

        Returns the extracted PDF text. The on-page HTML notice is an OCR
        conversion that is truncated, so the PDF text is used as the
        canonical (full) source text whenever a PDF is available.
        """
        from urllib.parse import urljoin
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
        abs_href = urljoin(page.url, href)
        try:
            resp = page.request.get(abs_href, timeout=60000)
            data = resp.body()
        except Exception as e:
            logger.warning("PDF download failed for %s: %s", notice_id, e)
            return None
        text = extract_pdf_text(data)
        if text:
            logger.debug("PDF for %s: %d chars", notice_id, len(text))
        return text

    def _extract_notice_text(
        self, page, session_id: str, pk_id: str, site_key: Optional[str] = None
    ) -> Optional[str]:
        """Navigate to a detail page, pass the Turnstile gate, and return the
        canonical notice text (PDF preferred, else the trimmed on-page notice).

        Returns ``None`` if the detail page is unreachable or the body is too
        short to be a real notice.
        """
        site_key = site_key or self.TURNSTILE_SITE_KEY
        detail_url = f"{self.BASE_URL}/(S({session_id}))/Details.aspx?SID={session_id}&ID={pk_id}"
        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning("Failed to load detail page: %s", e)
            return None
        page.wait_for_timeout(3000)

        if not self._pass_turnstile_gate(page, site_key):
            logger.warning("detail content unavailable for %s", pk_id)
            return None

        notice_text = page.evaluate(
            "() => (document.body ? document.body.innerText : '') || ''"
        )
        if len(notice_text) < 500:
            logger.warning("Content too short (%d chars) for %s", len(notice_text), pk_id)
            return None

        notice_text = trim_notice_body(notice_text)
        pdf_text = self._fetch_pdf_text(page, pk_id)
        return pdf_text or notice_text

    # ---- shared classifiers -------------------------------------------------

    @staticmethod
    def _is_mortgage_foreclosure(text: str) -> bool:
        """True only when *text* is clearly a mortgage/deed-of-trust (bank) sale.

        Used as a cheap grid-level pre-filter so we don't spend a Turnstile
        solve on notices that are obviously not tax foreclosures. Ambiguous
        notices (no clear signal either way) are NOT flagged here -- they are
        passed through to the detail-level check.
        """
        if not text:
            return False
        low = normalize_notice_text(text).lower()
        has_mortgage = any(re.search(p, low) for p in MORTGAGE_FC_PATTERNS)
        has_tax = any(re.search(p, low) for p in TAX_FC_PATTERNS)
        return has_mortgage and not has_tax

    @staticmethod
    def _is_tax_foreclosure(text: str) -> bool:
        """Authoritative check: is *text* a tax foreclosure / tax sale notice?

        Keeps county-trustee sales for delinquent property taxes. A
        deed-of-trust / substitute-trustee bank sale is rejected unless it also
        carries an explicit tax-sale signal.
        """
        if not text:
            return False
        low = normalize_notice_text(text).lower()
        has_mortgage = any(re.search(p, low) for p in MORTGAGE_FC_PATTERNS)
        has_tax = any(re.search(p, low) for p in TAX_FC_PATTERNS)
        if has_mortgage and not has_tax:
            return False
        if has_mortgage:
            # A deed-of-trust / substitute-trustee sale is a private-lender
            # (mortgage) foreclosure. Only accept it as a tax sale when it also
            # carries a STRONG tax signal (county-trustee sale for delinquent
            # property taxes under T.C.A. 67-5).
            return any(re.search(p, low) for p in STRONG_TAX_FC_PATTERNS)
        return has_tax
