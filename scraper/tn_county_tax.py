"""Blount County (TN) delinquent property tax sale scraper.

The Blount County Clerk & Master runs ONE sale per year (June) and
publishes the parcel list as a CivicPlus DocumentCenter PDF linked from
https://www.blounttn.gov/2033/Properties-for-Sale ("Properties for Sale
(updated <date>) (PDF)"). The list goes up ~1 month before the sale and
is updated weekly (daily during sale week); it never appears on
tnpublicnotice.com, so this page is the only machine-readable source.

PDF layout (verified live, Sale 33 / June 4 2026):
  page 1   "Tax Sale: 33 / Date of Sale: June 4, 2026 / Time: 10:01 a.m. /
            Location: Heritage High School ..."
  pages 2+ numbered per-property blocks:
    "1. Property described on Property Assessor's Map for Blount County,
        Tennessee, as Parcel No. 06-077N-A-077N-056.00 located in Blount
        County, Tennessee, assessed to Larry F. Adams, for 2023 County
        taxes, together with interest, penalty, fees and costs in the
        total amount of $865.02."
    notice case number on the notice head: "No. 2025-CH-44"

The PDF is a scan (no text layer) — OCR via pdftoppm+tesseract, same
approach as scraper/haywood_county.py. OCR cost scales with the list
(31 pages for Sale 33) and only happens when the interface page shows a
NEW "(updated ...)" version; unchanged versions re-emit cached rows.

Row lifecycle / REAP:
  * Fresh stamp + upcoming sale  → rows published; a parcel that drops
    off a weekly update is archived by ```
    reap_missing_listings(source="blount_tax")`` (registered REAP source).
  * Stale stamp (>90 days old — last sale held) or a parsed sale date in
    the past → successful run that OBSERVES NOTHING: ``observed_listing_ids
    = set()`` is the explicit "sale realized" signal; db.EMPTY_REAP_ALLOWED
    lets that empty sighting archive the previous sale's rows.
  * Interface page unreachable   → ``observed_listing_ids = None``
    (no signal — REAP protected, never reaps on an outage).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from .base import BaseScraper, PropertyData, camoufox_context
from .config import config

logger = logging.getLogger(__name__)

PROPS_PAGE_URL = "https://www.blounttn.gov/2033/Properties-for-Sale"
DOC_CENTER_PREFIX = "https://www.blounttn.gov"

# "Properties for Sale (updated June 03, 2026) (PDF)" link text
_PDF_LINK_RE = re.compile(
    r"Properties for Sale\s*\(updated\s+(?P<stamp>[^)]+?)\)\s*\(PDF\)", re.I)
_DOC_ID_RE = re.compile(r"/DocumentCenter/View/(\d+)")

# OCR'd notice fields
_SALE_DATE_RE = re.compile(r"Date of Sale:\s*([A-Z][a-z]+ \d{1,2},? \d{4})", re.I)
_TAX_SALE_NO_RE = re.compile(r"Tax Sale:\s*(\d+)")
_CASE_RE = re.compile(r"\bNo\.\s*(\d{4}-CH-\d+)\b")
_PARCEL_RE = re.compile(r"Parcel No\.\s*([0-9]{2,4}(?:[-–][0-9A-Za-z.]*)*)")
_OWNER_RE = re.compile(r"assessed to\s+(.+?),?\s+(?:for \d{4}|now|,?\s*now)", re.I)
_AMOUNT_RE = re.compile(r"total amount of\s*\$?\s*([\d,]+(?:\.\d{2})?)", re.I)

_CACHE_PATH = Path(__file__).parent / "tmp" / "blount_tax_cache.json"


# ---------------------------------------------------------------------------
# OCR (scanned PDF -> text), same approach as haywood_county.py
# ---------------------------------------------------------------------------

def _ocr_pdf_bytes(data: bytes) -> Optional[str]:
    """OCR a scanned notice PDF via pdftoppm (PNG render) + tesseract."""
    try:
        import shutil
        if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
            logger.error("Blount OCR unavailable (pdftoppm/tesseract missing)")
            return None
        with tempfile.TemporaryDirectory() as td:
            pdf_path = os.path.join(td, "scan.pdf")
            with open(pdf_path, "wb") as f:
                f.write(data)
            # 200 dpi covers Blount's large type; 300 dpi is the slow
            # fallback for small print.
            for dpi in (200, 300):
                prefix = os.path.join(td, f"p{dpi}")
                subprocess.run(
                    ["pdftoppm", "-r", str(dpi), "-png", pdf_path, prefix],
                    check=True, timeout=1800)
                texts: List[str] = []
                for png in sorted(os.listdir(td)):
                    if not png.startswith(f"p{dpi}"):
                        continue
                    r = subprocess.run(
                        ["tesseract", os.path.join(td, png),
                         "stdout", "--psm", "6"],
                        capture_output=True, text=True, timeout=300)
                    texts.append(r.stdout or "")
                joined = "\n".join(texts)
                if len(joined.strip()) > 3000:
                    return joined
                logger.warning("Blount OCR @ %ddpi produced too little text "
                               "(%d chars)", dpi, len(joined.strip()))
            return None
    except Exception as e:
        logger.error("Blount OCR failed: %s", e)
        return None


def _pdf_text(data: bytes) -> Optional[str]:
    """Extract text layer when present, else OCR."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        if text and len(text.strip()) > 500:
            return text
    except Exception as e:
        logger.warning("Blount pdfplumber extraction failed (scanned?): %s", e)
    return _ocr_pdf_bytes(data)


# ---------------------------------------------------------------------------
# Parsing helpers (module-level for tests)
# ---------------------------------------------------------------------------

def _slug_words(text: str) -> str:
    slug = (text or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "list"


def _stamp_is_stale(stamp: str, max_age_days: int = 90) -> bool:
    """'June 03 2026' older than `max_age_days` -> last sale is over."""
    raw = (stamp or "").replace(",", " ").strip()
    for fmt in ("%B %d %Y", "%m/%d/%Y", "%B %d, %Y"):
        try:
            return (date.today() - datetime.strptime(raw, fmt).date()).days > max_age_days
        except ValueError:
            continue
    return False


def _parse_sale_header(text: str) -> dict:
    """Page-1 fields: sale number, ISO sale date, location, case."""
    out: dict = {}
    m = _TAX_SALE_NO_RE.search(text)
    if m:
        out["sale_no"] = m.group(1)
    m = _SALE_DATE_RE.search(text)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            out["sale_date"] = datetime.strptime(raw, "%B %d %Y").date().isoformat()
        except ValueError:
            pass
    m = _CASE_RE.search(text)
    if m:
        out["court_case"] = m.group(1)
    m = re.search(r"Location:\s*(.+)", text)
    if m:
        out["location"] = m.group(1).strip().split("\n")[0].strip()
    return out


def _parse_property_blocks(text: str) -> List[dict]:
    """Split pages-2+ text into one dict per 'Parcel No.' block."""
    out: List[dict] = []
    for block in re.split(r"(?=\d+\.\s+Property described on)", text):
        if "Parcel No." not in block:
            continue
        pm = _PARCEL_RE.search(block)
        if not pm:
            continue
        parcel = (pm.group(1).replace("–", "-").replace("—", "-").strip()
                  .rstrip("."))
        owner = None
        om = _OWNER_RE.search(block)
        if om:
            owner = om.group(1).strip().strip(",")
        amount = None
        am = _AMOUNT_RE.search(block)
        if am:
            try:
                amount = float(am.group(1).replace(",", ""))
            except ValueError:
                amount = None
        cm = _CASE_RE.search(block)
        out.append({
            "parcel": parcel,
            "owner": owner,
            "amount": amount,
            "court_case": cm.group(1) if cm else None,
            "raw": re.sub(r"\s+", " ", block).strip()[:1200],
        })
    return out


def _build_rows(source: str, header: dict, blocks: List[dict],
                page_url: str) -> List["PropertyData"]:
    """One tax_foreclosure row per parcel block."""
    sale_date = header.get("sale_date")
    case = header.get("court_case") or (blocks[0].get("court_case") if blocks else None)
    rows: List[PropertyData] = []
    for b in blocks:
        parcel = b["parcel"]
        amount = b.get("amount")
        # Address-less rows: no GIS/Maps links (link-depth policy).
        rows.append(PropertyData(
            source=source,
            source_listing_id=f"sale{header.get('sale_no', '')}-{_slug_words(parcel)}",
            url=page_url,
            address=None,
            city=None,
            county="Blount",
            state="TN",
            zip_code=None,
            latitude=None,
            longitude=None,
            price=amount,
            acres=None,
            description=(
                f"Blount County TN delinquent property tax sale — "
                f"Parcel {parcel}"
                + (f" | {b['owner']}" if b.get("owner") else "")
                + (f" | Amount due: ${amount:,.2f}" if amount else "")
                + (f" | Sale {sale_date}" if sale_date else "")),
            property_type="tax_foreclosure",
            image_url=None,
            parcel_number=parcel,
            gis_url=None,
            auction_date=sale_date,
            close_date=None,
            upset_bid=None,
            foreclosure_key="|".join([case or "", parcel, str(amount or "")]),
            court_case=case,
            initial_auction_date=sale_date,
            upset_bid_end=None,
            google_maps_url=None,
            google_maps_topo_url=None,
            raw_source_text=b.get("raw"),
            raw_paragraph=b.get("raw"),
        ))
    return rows


# ---------------------------------------------------------------------------
# Cache: parsed rows keyed by DocumentCenter doc id, so an unchanged
# "(updated ...)" PDF re-emits without another OCR pass.
# ---------------------------------------------------------------------------

def _cache_load() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_save(doc_id: str, stamp: str, url: str,
                sale_over: bool, rows: List[PropertyData]) -> None:
    _CACHE_PATH.parent.mkdir(exist_ok=True)
    cache = _cache_load()
    cache[doc_id] = {"stamp": stamp, "url": url, "sale_over": sale_over,
                     "rows": [dict(r) for r in rows]}
    _CACHE_PATH.write_text(json.dumps(cache))


class BlountTaxScraper(BaseScraper):
    """Blount County (TN) annual delinquent property tax sale list."""

    SOURCE_NAME = "blount_tax"
    BASE_URL = PROPS_PAGE_URL
    MIN_ACREAGE = config.MIN_ACRES
    # REAP contract: None = no signal (outage); set() = successful run that
    # observed nothing (sale realized); non-empty = ids seen this run.
    observed_listing_ids: Optional[set]

    def __init__(self, delay_range: tuple = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)
        self.observed_listing_ids = None

    # ---- interface page ---------------------------------------------------

    def _fetch_interface(self) -> Optional[tuple]:
        """(update_stamp, pdf_url) from the Properties-for-Sale page.

        The page lists ONLY one current "Properties for Sale (updated X)
        (PDF)" link; anything else is stale junk. Returns None on failure.
        """
        try:
            with camoufox_context() as page:
                page.goto(PROPS_PAGE_URL, timeout=60000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                html = page.content()
        except Exception as e:
            logger.error("Blount interface fetch failed: %s", e)
            return None
        matches: List[tuple] = []
        for m in re.finditer(
                r"<a[^>]+href=\"([^\"]*?/DocumentCenter/View/[^\"]+)\"[^>]*>"
                r"([^<]{5,200})</a>", html):
            href, link_text = m.group(1), m.group(2)
            lm = _PDF_LINK_RE.search(link_text)
            if lm and re.search(r"Properties for Sale", link_text, re.I):
                matches.append((lm.group("stamp").strip(), href))
        if not matches:
            logger.warning("Blount: no Properties-for-Sale PDF link found "
                           "(layout change?)")
            return None
        stamp, href = matches[0]
        if not href.startswith("http"):
            href = DOC_CENTER_PREFIX + href
        return stamp, href

    # ---- scrape -----------------------------------------------------------

    def scrape(self) -> List[PropertyData]:
        interface = self._fetch_interface()
        if not interface:
            return []
        stamp, pdf_url = interface
        doc_id_m = _DOC_ID_RE.search(pdf_url)
        doc_id = doc_id_m.group(1) if doc_id_m else _slug_words(stamp)
        cache = _cache_load().get(doc_id, {})
        cached_rows: List[PropertyData] = [dict(r) for r in cache.get("rows", [])]

        stale = _stamp_is_stale(stamp)
        if stale or cache.get("sale_over"):
            # Page still shows the previous sale's list. A successful scrape
            # that sees only a stale list means the sale realized — signal REAP.
            logger.info("Blount: list '%s' is stale (sale over) — realized signal",
                        stamp)
            self.observed_listing_ids = set()
            return []

        if cached_rows and not stale:
            logger.info("Blount: re-emitting %d cached rows (%s)",
                        len(cached_rows), stamp)
            self.observed_listing_ids = {
                r["source_listing_id"] for r in cached_rows}
            return cached_rows

        # Fresh version — download + text/OCR + parse.
        from .county_static import download_bytes
        data = download_bytes(pdf_url)
        if not data or data[:4] != b"%PDF":
            logger.error("Blount: PDF download failed or non-PDF (%s)", pdf_url)
            return []  # no interface change: leave previous observed intact
        text = _pdf_text(data)
        if not text:
            self.observed_listing_ids = set()
            return []
        header = _parse_sale_header(text)
        blocks = _parse_property_blocks(text)
        if not header or not blocks:
            logger.warning("Blount: OCR text yielded no sale header/blocks "
                           "(%d chars text)", len(text))
            self.observed_listing_ids = set()
            return []
        if header.get("sale_date") and _date_in_past(header["sale_date"]):
            logger.info("Blount sale %s held %s (past) — realized signal",
                        header.get("sale_no"), header["sale_date"])
            _cache_save(doc_id, stamp, pdf_url, True, [])
            self.observed_listing_ids = set()
            return []

        rows = _build_rows(self.SOURCE_NAME, header, blocks, PROPS_PAGE_URL)
        _cache_save(doc_id, stamp, pdf_url, False, rows)
        self.observed_listing_ids = {r["source_listing_id"] for r in rows}
        logger.info("Blount sale %s (%s): %d parcels published",
                    header.get("sale_no"), header.get("sale_date"), len(rows))
        return rows


def _date_in_past(iso: str) -> bool:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date() < date.today()
    except ValueError:
        return False


def main() -> int:  # pragma: no cover
    props = BlountTaxScraper().run()
    print(f"Blount: {len(props)} parcels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
