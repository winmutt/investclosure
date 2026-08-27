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

import json
import logging
import time
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from .base import BaseScraper, PropertyData, camoufox_context, CamoufoxFetcher
from .config import config, NC_FORECLOSURE_COUNTIES

logger = logging.getLogger(__name__)

# NC mountain counties we care about
NC_FORECLOSURE_COUNTIES = {
    "alleghany", "ashe", "avery", "buncombe", "burke",
    "cherokee", "clay", "graham", "haywood",
    "henderson", "jackson", "madison", "mcdowell", "mitchell",
    "polk", "macon", "swain", "transylvania", "watauga", "yancey",
}

# Slug patterns that indicate real-property-relevant notices
PROPERTY_RELEVANT_SLUGS = [
    "foreclosure", "sale", "exchange", "trust", "lien", "mortgage",
    "distress", "sheriff", "execution", "attachment", "judicial",
    "creditor", "filing", "publication", "bidding",
]

# Citizen Times / Gannett NC Public Notices search API
CITIZEN_TIMES_API_URL = "https://www.citizen-times.com/public-notices/api/search"
CITIZEN_TIMES_STATE_FILE = "citizen_times_state.json"
CITIZEN_TIMES_BACKFILL_DAYS = 180      # initial 6-month lookback
CITIZEN_TIMES_REGULAR_DAYS = 7         # regular rolling window

# ---------------------------------------------------------------------------
# Tax-foreclosure classification.
#
# Local newspaper legal-notice feeds (Transylvania Times, Watauga Democrat,
# Sylva Herald, Mitchell News, Citizen Times) mix genuine county/city tax-sale
# foreclosures with a large amount of non-foreclosure junk: probate/creditor
# notices, NOTICE OF PUBLIC HEARING / bond-order notices, advertisement-for-
# bids / request-for-proposals (county procurement), and mortgage/deed-of-trust
# (bank) foreclosures. Only genuine *property-tax* foreclosure/sale notices are
# investable, so every sub-scraper gates on :func:`_is_tax_foreclosure_notice`.
#
# Positive tax-sale language (anchored on NCGS Chapter 105 and the "satisfy
# unpaid property taxes" phrasing used in NC tax foreclosures). Mirrors the
# tax regex in ``nc_publicnotice.py``.
_TAX_SALE_RE = re.compile(
    r"foreclosure\s+sale\s+to\s+satisfy\s+unpaid|"
    r"satisfy\s+unpaid\s+(?:property\s+)?taxes|"
    r"unpaid\s+property\s+taxes\s+owing|"
    r"taxes\s+owing\s+to|"
    r"\btax\s+foreclosure\b|"
    r"foreclosure\s+(?:of|for)\s+(?:the\s+)?tax|"
    r"tax\s+lien|lien\s+(?:for|of)\s+tax|"
    r"in\s+rem\s+foreclosure|"
    r"delinquent\s+(?:property\s+)?tax|"
    r"delinquent\s+ad\s+valorem|"
    r"commissioner\s+of\s+(?:revenue|taxes)|"
    r"chapter\s+105|\bgs\s*105\b|"
    r"general\s+statute[s]?\s+(?:chapter\s+)?105|"
    r"tax\s+sale|certificate\s+of\s+tax|"
    r"unpaid\s+(?:property\s+)?tax|"
    r"lien\s+for\s+tax",
    re.IGNORECASE,
)

# Mortgage / deed-of-trust (bank) sales. Present alone these are NOT
# tax foreclosures (they sell to satisfy a loan, not unpaid property taxes).
_MORTGAGE_SALE_RE = re.compile(
    r"deed\s+of\s+trust|substitute\s+trustee|"
    r"trustee'?s?\s+sale|power\s+of\s+sale|"
    r"foreclosure\s+of\s+a\s+deed|notice\s+of\s+trustee'?s?\s+sale|"
    r"pursuant\s+to\s+(?:a\s+|the\s+)?deed\s+of\s+trust|"
    r"holder\s+of\s+the\s+(?:note|deed)",
    re.IGNORECASE,
)

# Probate / creditor / administration notices ("having qualified as Executor",
# NOTICE TO CREDITORS, NOTICE OF ADMINISTRATION). These are estate proceedings,
# not property sales.
_PROBATE_RE = re.compile(
    r"having\s+qualified\s+as\s+(?:executor|executrix|administrator|administratrix|"
    r"personal\s+representative|trustee)|"
    r"executor(?:s)?\s+of\s+the\s+estate|"
    r"notice\s+to\s+creditors|notice\s+of\s+administration|"
    r"estate\s+file|probate|deceased\s+late\s+of|"
    r"creditor'?s\?*\s+notice",
    re.IGNORECASE,
)

# County/municipal government procurement & public-hearing notices (bond
# orders, grant applications, parks/transportation projects, RFP / sealed
# proposals). Not property foreclosures.
_GOVERNMENT_RE = re.compile(
    r"notice\s+of\s+public\s+hearing|public\s+hearing\s+on|"
    r"advertisement\s+for\s+bids|request\s+for\s+(?:proposals|bids|quotes)|\brfp\b|\bifb\b|"
    r"sealed\s+proposals?|sealed\s+bids?|"
    r"bond\s+order|general\s+obligation\s+(?:school\s+)?bonds?|"
    r"grant\s+application|community\s+development\s+block|community\s+transportation",
    re.IGNORECASE,
)

# Miscellaneous legal / procedural filings that are not upcoming tax sales.
_MISC_FILING_RE = re.compile(
    r"notice\s+of\s+service\s+of\s+process|service\s+by\s+process\s+by\s+publication|"
    r"order\s+for\s+service\s+by\s+publication|"
    r"name\s+change|notice\s+of\s+intent\s+to\s+file|"
    r"foreclosure\s+of\s+equity\s+of\s+redemption|excess\s+funds|surplus\s+funds|"
    r"interpleader|quiet\s+title|establish\s+title\s+against\s+all\s+the\s+world|"
    r"tax\s+sale\s+redemption",
    re.IGNORECASE,
)


def _is_tax_foreclosure_notice(text: str) -> bool:
    """Return True only for a genuine NC property-tax foreclosure / sale notice.

    Requires positive tax-sale language (NCGS Chapter 105, "foreclosure sale
    to satisfy unpaid property taxes", "tax foreclosure/sale/lien", delinquent
    ad valorem, etc.). Rejects outright:
      - probate / creditor / administration notices,
      - county/municipal public hearing & bid/procurement notices,
      - quiet-title / tax-redemption / excess-fund / service-by-publication
        procedural filings,
      - mortgage / deed-of-trust (bank) sales that carry no tax-sale language.
    """
    if not text:
        return False
    low = text.lower()
    if _PROBATE_RE.search(low) and not _TAX_SALE_RE.search(low):
        return False
    if _GOVERNMENT_RE.search(low) and not _TAX_SALE_RE.search(low):
        return False
    if _MISC_FILING_RE.search(low) and not _TAX_SALE_RE.search(low):
        return False
    if not _TAX_SALE_RE.search(low):
        return False
    # Strong tax-sale language present. A deed-of-trust / power-of-sale bank
    # sale is still rejected unless it carries an explicit tax-sale signal
    # (tax-lien foreclosures by substitute trustee do).
    if _MORTGAGE_SALE_RE.search(low) and not _TAX_SALE_RE.search(low):
        return False
    return True


def _slug_to_title(slug: str) -> str:
    if not slug:
        return "Unknown"
    return " ".join(slug.replace("-", " ").title().split())


def _citizen_times_state_path() -> Path:
    """Path to the JSON state file tracking the last successful lookback end."""
    return config.data_dir / CITIZEN_TIMES_STATE_FILE


def _read_citizen_times_state() -> Optional[str]:
    """Return the last successful end date (ISO) from state, or None."""
    try:
        p = _citizen_times_state_path()
        if p.exists():
            data = json.loads(p.read_text())
            return data.get("last_end")
    except Exception as exc:
        logger.warning("citizen-times state read failed: %s", exc)
    return None


def _write_citizen_times_state(end_date: str) -> None:
    """Persist the last successful end date so the next run is incremental."""
    try:
        p = _citizen_times_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_end": end_date}, indent=2))
    except Exception as exc:
        logger.warning("citizen-times state write failed: %s", exc)


def _extract_notice_county(text: str, slug: str = "") -> Optional[str]:
    """Extract the NC county name from a public-notice text body."""
    if text:
        m = re.search(r"COUNTY\s+OF\s+([A-Z][A-Z]+)", text, re.IGNORECASE)
        if m:
            return m.group(1).title()
        m = re.search(r"(\b[A-Z][A-Z]+)\s+COUNTY\b", text)
        if m and m.group(1) != "NORTH":
            return m.group(1).title()
        m = re.search(r"late\s+of\s+([A-Za-z]+)\s+County", text, re.IGNORECASE)
        if m:
            return m.group(1).title()
        m = re.search(
            r"THE\s+GENERAL\s+COURT\s+OF\s+JUSTICE[^\n]*?\n\s*[A-Z]+.*?\n\s*([A-Z][A-Z]+)\s+COUNTY",
            text,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).title()
    if slug:
        m = re.search(r"-([a-z]+)-county-", slug)
        if m:
            return m.group(1).title()
    return None


def _extract_notice_address(text: str) -> Optional[str]:
    """Extract 'Address of Property: 328 Wooten Cove Rd.' style addresses."""
    m = re.search(r"Address\s+of\s+(?:the\s+)?Property\s*:?\s*([^\n]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().strip(".,")
    return None


def _extract_auction_date(text: str) -> Optional[str]:
    """Extract a 'Date of Sale: August 18, 2026' auction date as ISO."""
    m = re.search(
        r"(?:Date\s+of\s+Sale|sale\s+date)\s*:?\s*"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s+(\d{4})",
        text,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"\bon\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s+(\d{4})"
            r"\s+(?:at\s+\d{1,2}:\d{2}\s*[AP]M)?",
            text,
            re.IGNORECASE,
        )
    if not m:
        return None
    month = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }[m.group(1)[:3].title()]
    try:
        day, year = int(m.group(2)), int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def _try_citizen_times(lookback_days: Optional[int] = None) -> list[PropertyData]:
    """Scrape foreclosure notices from the Citizen Times / Gannett API.

    Uses keyword='foreclosure' so probate/creditor notices (like the old
    NOTICE TO CREDITORS records) are excluded. The first run backfills
    ``CITIZEN_TIMES_BACKFILL_DAYS`` (180); later runs use a rolling
    ``CITIZEN_TIMES_REGULAR_DAYS`` (7) window starting from the last
    successful run, persisted to ``citizen_times_state.json``.
    """
    last_end = _read_citizen_times_state()
    if lookback_days is None:
        lookback_days = CITIZEN_TIMES_REGULAR_DAYS if last_end else CITIZEN_TIMES_BACKFILL_DAYS

    today = date.today()
    end_date = today.isoformat()
    start_date = (today - timedelta(days=lookback_days)).isoformat()

    logger.info("Citizen Times: fetching 'foreclosure' notices %s .. %s", start_date, end_date)

    properties: list[PropertyData] = []
    scraper = NewspaperNoticesScraper()
    page, total = 1, None
    seen: set[str] = set()
    try:
        from .base import camoufox_context, CamoufoxFetcher
        with camoufox_context() as cpage:
            fetcher = CamoufoxFetcher(cpage)
            # Navigate to the API origin once so the POST is same-origin.
            cpage.goto("https://www.citizen-times.com/public-notices",
                       wait_until="domcontentloaded", timeout=60000)
            cpage.set_extra_http_headers({
                "Content-Type": "text/plain;charset=UTF-8",
                "Referer": "https://www.citizen-times.com/public-notices",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            })
            while total is None or len(seen) < total:
                body = {
                    "publication": None,
                    "markets": None,
                    "keyword": "foreclosure",
                    "noticeType": "",
                    "state": "North Carolina",
                    "startDate": start_date,
                    "endDate": end_date,
                    "page": page,
                }
                raw = fetcher.post(CITIZEN_TIMES_API_URL, json.dumps(body))
                if not raw:
                    logger.warning("Citizen Times: empty API response on page %d", page)
                    break
                data = json.loads(raw)
                hits = (data.get("hits") or {}).get("hits") or []
                total = ((data.get("hits") or {}).get("total") or {}).get("value", 0)
                if not hits:
                    break
                for hit in hits:
                    src = hit.get("_source") or {}
                    nid = src.get("id")
                    text = src.get("text") or ""
                    if not nid or nid in seen:
                        continue
                    seen.add(nid)
                    slug = src.get("slug") or ""
                    county = _extract_notice_county(text, slug)
                    if not county or county.lower() not in NC_FORECLOSURE_COUNTIES:
                        continue
                    if not _is_tax_foreclosure_notice(f"{_slug_to_title(slug)} {text}"):
                        continue
                    case = scraper._extract_court_case(text)
                    pin = scraper._extract_pin(text)
                    deed_plat = scraper._extract_deed_plat(text)
                    addr = _extract_notice_address(text)
                    auction = _extract_auction_date(text)
                    if auction is None:
                        auction = src.get("date_start")
                    title = _slug_to_title(slug)
                    parts = [part for part in [
                        title,
                        f"Case: {case}" if case else None,
                        f"Auction: {auction}" if auction else None,
                        f"PIN: {pin}" if pin else None,
                        f"Deed/Plat: {deed_plat}" if deed_plat else None,
                    ] if part]
                    properties.append({
                        "source": "newspaper_notices",
                        "source_listing_id": nid,
                        "court_case": case,
                        "extracted_deed_plat": deed_plat,
                        "extracted_pin": pin,
                        "deed_book": deed_plat if (deed_plat or "").startswith("Deed:") else None,
                        "raw_source_text": text,
                        "raw_paragraph": text,
                        "url": "https://www.citizen-times.com/public-notices/",
                        "address": addr,
                        "city": None,
                        "county": county.title(),
                        "state": "NC",
                        "zip_code": None, "latitude": None, "longitude": None,
                        "price": None, "acres": None,
                        "description": f"[Citizen Times] {' -- '.join(parts)}",
                        "property_type": "public_notice", "image_url": None,
                        "parcel_number": pin,
                        "auction_date": auction, "close_date": None,
                    })
                page += 1
                time.sleep(0.5)
    except Exception as exc:
        logger.error("Citizen Times search failed: %s", exc)
        return properties

    _write_citizen_times_state(end_date)
    logger.info("Citizen Times: %d mountain-county foreclosure notices", len(properties))
    return properties


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
            self._scrape_citizen_times,
        ]:
            try:
                props = scrape_fn()
                all_properties.extend(props)
            except Exception as e:
                logger.warning("%s failed: %s", scrape_fn.__name__, e)

        # Deduplicate by (source_listing_id, url) so notices sharing a base
        # URL (citizen-times, Mitchell News) are not collapsed together.
        seen: set[tuple] = set()
        unique: list[PropertyData] = []
        for p in all_properties:
            key = (p.get("source_listing_id") or "", p.get("url") or "")
            if key not in seen:
                seen.add(key)
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

    # ── Parcel / PIN / deed / plat extraction helpers ─────────────────────

    @staticmethod
    def _normalize_pin(raw: str) -> str:
        """Normalize PIN separators: en-dash/'?'/spaces -> hyphens."""
        return re.sub(r"[\s\u2013?]+", "-", raw.strip().strip(",")).strip("-")

    def _extract_pin(self, text: str) -> Optional[str]:
        """Return the tax parcel / PIN referenced in *text*, or None.

        Handles the formats seen in our county notices:
          "Parcel ID #9508-82-4582-000"          (Transylvania Times)
          "parcel ID 7567-93-8054"                (Jackson/Sylva Herald)
          "parcel identification number 9775-39-2342-00000"  (Buncombe)
          "PIN 9738-38-5063" / "PIN: 061878609600000"
          "Parcel #1984-32-8523-000"              (Watauga)
        """
        patterns = [
            # "tax parcel #1984-32-8523-000"
            r"[Tt]ax\s+parcel\s*#?\s*(\d{3,}-[\d\u2013\-?]+)",
            # "Parcel ID #9508-82-4582-000" | "parcel ID 7567-93-8054"
            r"[Pp]arcel\s+ID\s*[:#]?\s*(\d{3,}-[\d\u2013\-?]+)",
            # "parcel identification number 9775-39-2342-00000 and 9775-39-0376-00000"
            r"[Pp]arcel\s+[Ii]dentification\s+[Nn]umber\s*[:#]?\s*"
            r"((?:\d[\d\u2013\-? ]*?))(?=\s+and\s|\s*[;,.]|\s+$)",
            # "Parcel #1984-32-8523-000" | "Parcel:1984-32-8523-000"
            r"[Pp]arcel\s*[:#]\s*(\d{3,}-[\d\u2013\-?]+)",
            # "Parcel 1984-32-8523-000"
            r"[Pp]arcel\s+(\d{3,}-[\d\u2013\-?]+)",
            # "PIN 9738-38-5063" | "PIN: 061878609600000" | "PIN 9775-39-2342?00000"
            r"\bPIN\s*[:#]?\s*(\d{4,}(?:-[\d\u2013\-?]+)+|\d{12,})",
            # "PID: 1984-42-0606-000"
            r"\bPID\s*[:#]?\s*(\d{3,}-[\d\u2013\-?]+)",
            # "REID 12345" (NC tax deed)
            r"\bREID\s*[:#]?\s*(\d{4,})",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return self._normalize_pin(m.group(1))
        return None

    def _extract_deed_plat(self, text: str) -> Optional[str]:
        """Return a deed / plat book reference, e.g. 'Deed:Bk1341Pg599' / 'Plat:File15Pg282'."""
        # Deed: "Deed Book 1341, Page 599" | "Deed Book 794 at Page 609" | "Deed Vol 1234 Page 567"
        m = re.search(
            r"[Dd]eed\s+(?:[Bb]ook|[Bb]k|[Vv]ol\.?|[Vv]olume)\s+(\d+)\s*,?\s*"
            r"(?:at\s+)?(?:[Pp]age|[Pp]g)\.?\s+(\d+)",
            text,
        )
        if m:
            return f"Deed:Bk{m.group(1)}Pg{m.group(2)}"

        # Plat book: "Plat Book 211, Page 72" | "Plat Book 11 Page 93"
        m = re.search(
            r"[Pp]lat\s+(?:[Bb]ook|[Bb]k)\s+(\d+)\s*,?\s*"
            r"(?:at\s+)?(?:[Pp]age|[Pp]g)\.?\s+(\d+)",
            text,
        )
        if m:
            return f"Plat:Bk{m.group(1)}Pg{m.group(2)}"

        # Plat file: "Plat File 15, Page 282" (Transylvania registry)
        m = re.search(
            r"[Pp]lat\s+[Ff]ile\s+(\d+)\s*,?\s*"
            r"(?:at\s+)?(?:[Pp]age|[Pp]g)\.?\s+(\d+)",
            text,
        )
        if m:
            return f"Plat:File{m.group(1)}Pg{m.group(2)}"

        # Plat cabinet: "Plat Cabinet 25 at Slide 378" (Jackson registry)
        m = re.search(
            r"[Pp]lat\s+[Cc]abinet\s+(\d+)\s*,?\s*(?:at\s+)?(?:[Ss]lide)\s+(\d+)",
            text,
        )
        if m:
            return f"Plat:Cabinet{m.group(1)}Slide{m.group(2)}"

        # Bare book/page used for recordings without a Deed/Plat prefix:
        # "recorded in Book 332 at Page 316" / "recorded in Book 1848, Page 510"
        # Also citizen-times style: "Book : 6481 Page: 631" / "Book: 655 Page: 432"
        m = re.search(
            r"[Bb]ook\s*:?\s+(\d+)\s*,?\s*(?:at\s+)?(?:[Pp]age|[Pp]g)\.?\s*:?\s+(\d+)",
            text,
        )
        if m:
            return f"Deed:Bk{m.group(1)}Pg{m.group(2)}"

        return None

    def _extract_court_case(self, text: str) -> Optional[str]:
        """Return the NC court case number referenced in *text*, or None.

        Format: YY + court division/type letters + sequence, e.g.
          "26CV000298-870"   (Buncombe District Court, civil)
          "26SP000450-100"   (Special Proceeding — foreclosure sales)
          "22CVD003028-100"  (District civil)
          "26JT000094-100"   (Juvenile/Trust, occasionally tax foreclosure)
        """
        # NC court case: 2-digit year + 2-3 letters + sequence, optional -suffix
        # Also handles citizen-times dashed-year form "25-CV015317-250"
        patterns = [
            r"\b(\d{2}(?:CVS|CVD|CVR|CV|SP|SPE|JT|JA|E)\d{3,6}(?:-\d{1,4})?)\b",
            r"\b(\d{2}-(?:CVS|CVD|CVR|CV|SP|SPE|JT|JA|E)\d{3,6}(?:-\d{1,4})?)\b",
            r"\b(?:Case (?:No|Number|#)\.?\s*:?\s*)(\d{2}-?[A-Z]{1,3}\d{3,6}(?:-\d{1,4})?)\b",
            r"\b(?:In the (?:General|District) Court[^,]*,\s*)(\d{2}-?[A-Z]{1,3}\d{3,6}(?:-\d{1,4})?)\b",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return m.group(1).strip().upper()
        return None

    def _extract_parcel(self, text: str) -> Optional[str]:
        """Backward-compatible alias for _extract_pin."""
        return self._extract_pin(text)

    @staticmethod
    def _is_unpaid_tax_notice(text: str) -> bool:
        """Backward-compatible alias for :func:`_is_tax_foreclosure_notice`.

        Restricts the newspaper feeds to genuine unpaid/delinquent property-tax
        foreclosure and tax-sale notices (see module docstring).
        """
        return _is_tax_foreclosure_notice(text)

    # ── Phase-2 helper: visit a single detail URL in an isolated tab ───────

    def _visit_detail(self, url: str) -> dict:
        """Open *url* in a fresh tab and return {parcel, pin, deed_plat, court_case, title, raw_text}."""
        parcel, title, deed_plat, court_case, raw_text = None, None, None, None, None
        try:
            with camoufox_context() as page:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Wait until body has meaningful content
                page.wait_for_function(
                    "() => { const b = document.querySelector('body'); return b && b.innerText && b.innerText.length > 500; }",
                    timeout=10000,
                )

                body_text = page.inner_text("body") or ""
                raw_text = body_text
                skip = (not body_text
                        or any(phrase in body_text.lower() for phrase in ["sorry", "error", "blocked", "404"]))
                parcel = None if skip else self._extract_pin(body_text)
                deed_plat = None if skip else self._extract_deed_plat(body_text)
                court_case = None if skip else self._extract_court_case(body_text)

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

        return {"parcel": parcel, "pin": parcel, "deed_plat": deed_plat, "court_case": court_case, "title": title, "raw_text": raw_text}

    # ── Phase-1 scrapers: return list of (url, card_data) from listing page ─

    def _scrape_transylvanian_times(self) -> list[PropertyData]:
        logger.info("Scraping Transylvania Times ...")
        url = "https://marketplace.transylvaniatimes.com/brevard-nc/public-notices/search"

        with camoufox_context() as page:
            page.set_viewport_size({"width": 1920, "height": 1080})
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
                    if not any(kw in title.upper() for kw in ("FORECLOS","SALE","TAX","NOTICE")):
                        continue
                    combined = f"{title} {content}".lower()
                    if not any(pat in combined for pat in PROPERTY_RELEVANT_SLUGS):
                        continue
                    candidates.append({"href": href, "title": title, "content": content, "posted": posted})
                except Exception:
                    pass
                page.wait_for_timeout(200)

        # Phase 2: visit each detail in its own tab
        properties: list[PropertyData] = []
        for c in candidates:
            d_url = urljoin(url, c["href"])
            time.sleep(1)
            detail = self._visit_detail(d_url)
            if detail.get("parcel") is None and not detail.get("title"):
                logger.warning("TT %s detail page failed or empty", c["href"][:40])
                continue
            base_title = detail["title"] if detail["title"] and len(detail["title"]) > 5 else c["title"]
            if not _is_tax_foreclosure_notice(f"{base_title} {detail.get('raw_text') or ''}"):
                logger.info("TT %s skipped (not a tax-foreclosure notice)", c["href"][:40])
                continue
            ad_id = c["href"].split("/")[-1] if "/" in c["href"] else f"tt_skip"
            desc = f"[Transylvania Times] {base_title}"
            if c["posted"]:
                desc += f" -- {c['posted']}"
            properties.append({
                "source": "newspaper_notices",
                "court_case": detail.get("court_case"),
                "extracted_deed_plat": detail.get("deed_plat"),
                "extracted_pin": detail.get("pin"),
                "deed_book": detail.get("deed_plat") if (detail.get("deed_plat") or "").startswith("Deed:") else None,
                "raw_source_text": detail.get("raw_text"),
                "raw_paragraph": detail.get("raw_text"),
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

        with camoufox_context() as page:
            page.set_viewport_size({"width": 1920, "height": 1080})
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
            if not _is_tax_foreclosure_notice(f"{base_title} {detail.get('raw_text') or ''}"):
                logger.info("WD %s skipped (not a tax-foreclosure notice)", c['slug'])
                continue
            desc = f"[Watauga Democrat] {base_title}"
            if c["date"]:
                desc += f" -- {c['date']}"
            properties.append({
                "source": "newspaper_notices",
                "court_case": detail.get("court_case"),
                "extracted_deed_plat": detail.get("deed_plat"),
                "extracted_pin": detail.get("pin"),
                "deed_book": detail.get("deed_plat") if (detail.get("deed_plat") or "").startswith("Deed:") else None,
                "raw_source_text": detail.get("raw_text"),
                "raw_paragraph": detail.get("raw_text"),
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

        with camoufox_context() as page:
            page.set_viewport_size({"width": 1920, "height": 1080})
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

        # Phase 2
        properties: list[PropertyData] = []
        skipped = 0
        for c in candidates:
            d_url = f"{base}{c['uid']}"  # uid already includes ad_ and .html
            time.sleep(2)
            detail = self._visit_detail(d_url)
            raw_text = detail.get("raw_text")
            if not raw_text:
                logger.warning("SH %s detail page failed or empty", c['uid'][:20])
                continue
            base_title = detail.get("title", "Legal Notice")
            if not base_title or len(base_title) < 4:
                base_title = "Legal Notice"
            # Restrict to UNPAID / DELINQUENT PROPERTY TAX notices only.
            if not self._is_unpaid_tax_notice(f"{base_title} {raw_text}"):
                logger.info("SH %s skipped (not an unpaid-tax notice)", c['uid'][:20])
                skipped += 1
                continue
            desc = f"[Sylva Herald] {base_title}"
            if c["date"]:
                desc += f" -- {c['date']}"
            properties.append({
                "source": "newspaper_notices",
                "court_case": detail.get("court_case"),
                "extracted_deed_plat": detail.get("deed_plat"),
                "extracted_pin": detail.get("pin"),
                "deed_book": detail.get("deed_plat") if (detail.get("deed_plat") or "").startswith("Deed:") else None,
                "raw_source_text": detail.get("raw_text"),
                "raw_paragraph": detail.get("raw_text"),
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
        logger.info("Sylva Herald: %d unpaid-tax notices (skipped %d non-tax)", len(properties), skipped)
        return properties

    def _scrape_mitchellnews(self) -> list[PropertyData]:
        logger.info("Scraping Mitchell News ...")
        url = "https://www.mitchellnews.com/classified/legals"

        with camoufox_context() as page:
            page.set_viewport_size({"width": 1920, "height": 1080})
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

        # Phase 2
        properties: list[PropertyData] = []
        skipped = 0
        for c in candidates:
            time.sleep(0.5)
            detail = self._visit_detail(url)  # Mitchell has no per-notice URLs
            base_title = detail.get("title", "Legal Notice") or "Legal Notice"
            if not _is_tax_foreclosure_notice(f"{base_title} {detail.get('raw_text') or ''}"):
                skipped += 1
                continue
            desc = f"[Mitchell News] {base_title}"
            if c["date"]:
                desc += f" -- {c['date']}"
            properties.append({
                "source": "newspaper_notices",
                "court_case": detail.get("court_case"),
                "extracted_deed_plat": detail.get("deed_plat"),
                "extracted_pin": detail.get("pin"),
                "deed_book": detail.get("deed_plat") if (detail.get("deed_plat") or "").startswith("Deed:") else None,
                "raw_source_text": detail.get("raw_text"),
                "raw_paragraph": detail.get("raw_text"),
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
        logger.info("Mitchell News: %d tax-foreclosure notices (skipped %d non-tax)", len(properties), skipped)
        return properties

    def _scrape_citizen_times(self, lookback_days: Optional[int] = None) -> list[PropertyData]:
        """Scrape Citizen Times / Gannett NC Public Notices search API."""
        return _try_citizen_times(lookback_days=lookback_days)
