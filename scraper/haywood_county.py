"""Haywood County (NC) tax foreclosure monitor — detail-aware.

The info page (https://www.haywoodcountync.gov/337/Tax-Foreclosures) carries
process text only. Live sales post to the county's CivicPlus bid module:

    https://www.haywoodcountync.gov/Bids.aspx?CatID=17&Status=open

Each posting is a detail page (bids.aspx?bidID=N) structured as standard
CivicPlus bid fields (Bid Title / Category / Status / Publication Date/Time /
Closing Date/Time) plus a Description block whose content is typically a
DocumentCenter link to the notice PDF. The notice PDFs are scanned images
(no text layer), so they are OCR'd via pdftoppm (300 dpi PNG render) +
tesseract; when a text layer exists pdfplumber is used instead. The full
notice text is kept as raw_source_text.

Parsed from the detail fields + notice text:
  - court case (26CV000653-430)          -> cs.find_case
  - PIN ("PIN 8711-00-6993")             -> cs.find_parcels
  - acreage ("containing 1.21 acres")    -> _extract_acreage (fallback only;
                                            GIS acres stay authoritative)
  - address ("... generally described* as 597 Laurel Branch Road,") -> clause-
    anchored extraction only (avoids attorney-firm footers)
  - auction date from detail Closing Date/Time
  - price when the notice carries a $ minimum bid

No captcha, no Turnstile. NC OneMap pre-insert acreage gate via
cs.enrich_and_filter_acres (GIS-first; notice acreage fills in only when the
GIS lookup can't resolve acres).
"""
from __future__ import annotations

import glob
import io
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Optional

from .base import BaseScraper, PropertyData, camoufox_context
from . import county_static as cs
from .rawlog import log_raw

logger = logging.getLogger(__name__)

INFO_URL = "https://www.haywoodcountync.gov/337/Tax-Foreclosures"
BIDS_URL = ("https://www.haywoodcountync.gov/Bids.aspx?CatID=17"
            "&txtSort=Category&showAllBids=&Status=open")
COUNTY = "Haywood"
MAX_POSTINGS = 15
BID_ID_RE = re.compile(r"bids\.aspx\?bidID=(\d+)", re.IGNORECASE)
DETAIL_URL = "https://www.haywoodcountync.gov/bids.aspx?bidID={bid}"

# Social-description acreage in plat boilerplate ("containing 1.21 acres").
ACREAGE_RE = re.compile(r"(?:containing|more or less)\s+(\d{1,4}(?:\.\d+)?)\s*acres",
                        re.IGNORECASE)
# Property-address clause: "generally described* as <street>, <City>, NC <zip>".
# Clause-anchored so law-firm footer addresses can never match.
ADDR_CLAUSE_RE = re.compile(
    r"\bdescribed[^.\n]{0,160}?as\s+"
    r"(\d{1,6}\s+[A-Za-z0-9 ./#'&-]+?),\s*"
    r"[A-Za-z .'-]+,\s*N\.?C\.?",
    re.IGNORECASE,
)


def _extract_acreage(text: str) -> Optional[float]:
    """First 'containing/more or less X acres' figure in *text*."""
    m = ACREAGE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _extract_address(text: str) -> Optional[str]:
    """'described ... as <street>, <City>, NC <zip>' street address, or None."""
    m = ADDR_CLAUSE_RE.search(re.sub(r"\s+", " ", text or ""))
    if not m:
        return None
    addr = m.group(1).strip(" .,#")
    # Require house number + street-type word so numerals from metes
    # ("34.72 feet") can't sneak through as street addresses.
    return addr if len(addr.split()) >= 3 else None


def _ocr_pdf(data: bytes) -> Optional[str]:
    """OCR a scanned notice PDF via pdftoppm (300dpi PNG) + tesseract."""
    try:
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="haywood_ocr_")
        pdf_path = os.path.join(tmpdir, "notice.pdf")
        prefix = os.path.join(tmpdir, "pg")
        with open(pdf_path, "wb") as f:
            f.write(data)
        subprocess.run(["pdftoppm", "-r", "300", "-png",
                        pdf_path, prefix], check=True)
        pages = sorted(glob.glob(f"{prefix}-*.png"))
        texts: list[str] = []
        for png in pages:
            r = subprocess.run(["tesseract", png, "stdout", "--psm", "4"],
                               capture_output=True, text=True)
            texts.append(r.stdout or "")
            try:
                os.unlink(png)
            except OSError:
                pass
        try:
            os.remove(pdf_path)
            os.rmdir(tmpdir)
        except OSError:
            pass
        return "\n".join(texts) if texts else None
    except FileNotFoundError as e:
        logger.warning("Haywood OCR unavailable (pdftoppm/tesseract missing): %s", e)
        return None
    except Exception as e:
        logger.warning("Haywood OCR failed: %s", e)
        return None


def _pdf_notice_text(data: bytes) -> Optional[str]:
    """Notice PDF text; falls back to OCR when the text layer is (nearly) empty."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        logger.warning("Haywood notice PDF parse failed: %s", e)
        text = ""
    if len(text.strip()) < 200:  # scanned notice — OCR it
        ocr = _ocr_pdf(data)
        if ocr and len(ocr.strip()) > len(text.strip()):
            text = ocr
    return text or None


def _parse_detail(url: str, wait_ms: int = 2500) -> Optional[dict]:
    """Load a CivicPlus bid detail page; return {fields, links, desc, body}.

    fields maps the bid-details table labels ("Bid Title:", ... , "Closing
    Date/Time:") to their values; links are the DocumentCenter/View hrefs in
    the Description block (the notice PDFs).
    """
    with camoufox_context() as page:
        page.set_viewport_size({"width": 1600, "height": 1000})
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        data = page.evaluate("""() => {
            const fields = {};
            let pending = null;
            document.querySelectorAll(
                'table[summary="Bid details"] td, table[summary="Bid Details"] td')
              .forEach(td => {
                  const text = (td.innerText || '').trim();
                  if (!text) return;
                  const first = text.split(/\\n/)[0].trim();
                  if (/:$/.test(first) && text.length < 70) {
                      pending = first.replace(/:$/, '');
                      const parts = text.split(/\\n/);
                      // label + value inside one cell
                      if (parts.length > 1 && parts.slice(1).join('').trim()) {
                          fields[pending] = parts.slice(1).join(' ').trim();
                          pending = null;
                      }
                      return;
                  }
                  if (pending) {
                      fields[pending] = text.replace(/\\n/g, ' ').trim();
                      pending = null;
                  }
              });
            const fr = document.querySelector('div.fr-view');
            const links = fr
                ? Array.from(fr.querySelectorAll('a'))
                      .map(a => a.href).filter(h => h)
                : [];
            const desc = fr ? (fr.innerText || '').trim() : '';
            return {fields, links, desc};
        }""") or {}
        try:
            body = page.inner_text("body") or ""
        except Exception:
            body = ""
        try:
            status = resp.status if resp else None
        except Exception:
            status = None
    out: dict = {"url": url, "body": body, "status": status,
                 "fields": data.get("fields") or {},
                 "links": data.get("links") or [],
                 "desc": data.get("desc") or ""}

    # Fallback: parse the body text when the DOM extraction is empty
    # (layout change).
    if not out["fields"]:
        out["fields"] = _parse_detail_body_fallback(body)
    return out


def _parse_detail_body_fallback(body: str) -> dict:
    """Parse bid fields from detail body text when the DOM table eval is empty
    (layout change). Labels are tab-separated from their values in inner_text
    ("Bid Title:\tTax Foreclosure sale ...")."""
    fields: dict[str, str] = {}
    for label in ("Bid Title", "Description", "Closing Date/Time",
                  "Publication Date/Time", "Publication Information",
                  "Category", "Status"):
        m = re.search(
            re.escape(label) + r"\s*[:=]\s*([^\n]+)", body or "", re.I)
        if m:
            fields[label] = m.group(1).strip()
    return fields


class HaywoodCountyScraper(BaseScraper):
    """Scrape Haywood County CivicPlus bid postings for tax foreclosure sales."""

    SOURCE_NAME = "haywood_county"
    BASE_URL = BIDS_URL

    def __init__(self, delay_range: tuple[float, float] = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)

    def scrape(self) -> list[PropertyData]:
        logger.info("Checking Haywood County bid postings ...")
        try:
            page = cs.fetch_page(BIDS_URL)
        except Exception as e:
            logger.error("Haywood County fetch failed: %s", e)
            return []
        html = page.get("html") or ""
        body = page.get("body") or ""
        bid_ids = re.findall(BID_ID_RE, html)
        # First-in-DOM order, unique. Detail pages have ?bidID= too, but the
        # list page is the only content loaded here.
        seen: set[str] = set()
        ordered = [b for b in bid_ids if not (b in seen or seen.add(b))]
        if not ordered:
            if re.search(r"no open bid postings|no .*bids? (available|at this time)",
                         body, re.I):
                cs.log_kept_empty(
                    self.SOURCE_NAME, COUNTY, "no open bid postings",
                    body[:2000], BIDS_URL,
                )
                logger.info("Haywood County: no open postings")
            else:  # page changed / anti-bot wall — alert via kept_empty
                cs.log_kept_empty(
                    self.SOURCE_NAME, COUNTY,
                    "list page yielded no bidID links",
                    body[:2000], BIDS_URL,
                )
            return []
        ordered = ordered[:MAX_POSTINGS]
        logger.info("Haywood County: %d open bid posting(s)", len(ordered))

        properties: list[PropertyData] = []
        notice_acres: dict[str, Optional[float]] = {}
        for bid in ordered:
            time.sleep(0.6)  # rate limit — 2 req/s across all HTTP calls
            url = DETAIL_URL.format(bid=bid)
            try:
                detail = _parse_detail(url)
            except Exception as e:
                logger.error("Haywood detail %s load failed: %s", bid, e)
                continue
            fields = detail.get("fields") or {}
            title = fields.get("Bid Title") or ""
            closing = fields.get("Closing Date/Time") or ""
            desc_text = detail.get("desc") or ""
            links = [l for l in detail.get("links") or []
                     if "DocumentCenter" in l]

            notice_text = ""
            doc_url = links[0] if links else None
            if doc_url:
                # Rate-limited before the PDF pull:
                time.sleep(0.6)
                try:
                    data = cs.download_bytes(doc_url)
                except Exception as e:
                    logger.warning("Haywood doc download failed: %s", e)
                    data = None
                if data and data[:5] == b"%PDF-":
                    notice_text = _pdf_notice_text(data) or ""
                elif data:
                    logger.info("Haywood doc %s is not a PDF (%dB, head %r)",
                                doc_url, len(data), data[:8])
            combined = "\n".join(
                part for part in (f"Bid Title: {title}",
                                  f"Closing: {closing}",
                                  desc_text, notice_text) if part)
            case = cs.find_case(combined)
            parcels = cs.find_parcels(combined)
            price = cs.parse_money(combined)
            sale_iso = (cs.to_iso_date(closing) if closing
                        else None)
            address = (_extract_address(notice_text or combined)
                       or None)
            notice_ac = _extract_acreage(notice_text or combined)

            lid = f"haywood_bid{bid}"
            if not (title or desc_text or notice_text):
                log_raw(
                    self.SOURCE_NAME, listing_id=lid, county=COUNTY,
                    state="NC", decision="kept_empty",
                    reason="detail page yielded no content", raw_text="",
                    url=url,
                )
                continue
            if len(parcels) > 1:  # one row per parcel (bundled notices)
                for idx, parcel in enumerate(parcels):
                    sub_lid = f"{lid}_{idx}"
                    notice_acres[sub_lid] = notice_ac
                    log_raw(
                        self.SOURCE_NAME, listing_id=sub_lid, county=COUNTY,
                        state="NC", decision="kept_tax",
                        reason=f"bid {bid} parcel {parcel}",
                        raw_text=combined, url=url,
                    )
                    properties.append(cs.build_tax_row(
                        source=self.SOURCE_NAME, listing_id=sub_lid, url=url,
                        county=COUNTY, parcel=parcel, address=address,
                        price=price, auction_date=sale_iso, court_case=case,
                        description=f"[Haywood Co] {title} — parcel {parcel}",
                        raw_text=combined, extra_key="bid",
                    ))
            else:
                notice_acres[lid] = notice_ac
                log_raw(
                    self.SOURCE_NAME, listing_id=lid, county=COUNTY,
                    state="NC", decision="kept_tax",
                    reason="bid posting detail", raw_text=combined, url=url,
                )
                properties.append(cs.build_tax_row(
                    source=self.SOURCE_NAME, listing_id=lid, url=url,
                    county=COUNTY,
                    parcel=parcels[0] if parcels else None, address=address,
                    price=price, auction_date=sale_iso, court_case=case,
                    description=f"[Haywood Co] {title}",
                    raw_text=combined, extra_key="bid",
                ))

        if not properties:
            cs.log_kept_empty(
                self.SOURCE_NAME, COUNTY,
                "open postings exist but none parsed into rows",
                body[:2000], BIDS_URL,
            )
            return []
        logger.info("Haywood County: %d row(s) from bid module", len(properties))
        props = cs.enrich_and_filter_acres(self.SOURCE_NAME, properties)
        return self._apply_notice_acres(props, notice_acres)

    def _apply_notice_acres(
        self, props: list[PropertyData],
        notice_acres: dict[str, Optional[float]],
    ) -> list[PropertyData]:
        """GIS-authoritative acreage.

        Notice-parsed acreage only fills rows the OneMap lookup couldn't
        resolve ("notice_text" source), with the same below-MIN drop rule
        log_raw'd like the GIS pass.
        """
        from .config import config
        min_acres = config.MIN_ACRES
        kept: list[PropertyData] = []
        for p in props:
            acres = notice_acres.get(p.get("source_listing_id") or "")
            if p.get("acres") is not None and acres is not None:
                continue  # GIS resolved; notice figure stays out of the way
            if acres is not None and acres < min_acres:
                log_raw(
                    self.SOURCE_NAME, listing_id=p.get("source_listing_id"),
                    county=p.get("county"), state="NC",
                    decision="dropped_below_min_acres",
                    reason=f"{acres:.2f}ac (notice) < MIN_ACRES {min_acres:g}",
                    raw_text=(p.get("raw_source_text") or "")[:2000],
                    url=p.get("url"),
                )
                logger.info("%s dropping %s: %.2fac (notice) below MIN_ACRES",
                            self.SOURCE_NAME, p.get("source_listing_id"), acres)
                continue
            if acres is not None and p.get("acres") is None:
                p["acres"] = acres
                p["acres_source"] = "notice_text"
            kept.append(p)
        return kept
