"""Ashe County (NC) tax foreclosure scraper.

The pending-foreclosures page is process info only:

    https://www.ashecountygov.com/departments/tax-administration/pending-tax-foreclosures

The actual parcel list is a linked PDF ("Pending Tax Foreclosures as of
<date>", nmcdn.io host) with columns: Parcel # | Address/Location |
Status | Sale Date | Minimum Bid.

Freshness guard: the linked PDF has gone stale before (2018 vintage), so
rows are only emitted when the PDF's own "Updated : <date>" stamp is
recent (within 365 days). Otherwise the run logs kept_empty and returns
[] — no 8-year-old rows spamming the DB/Telegram. When the county posts
a fresh PDF the parser picks it up with no code change.

No captcha, no Turnstile. NC OneMap enrichment via parcel in pipeline.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional

from .base import BaseScraper, PropertyData
from . import county_static as cs
from .rawlog import log_raw

logger = logging.getLogger(__name__)

PAGE_URL = (
    "https://www.ashecountygov.com/departments/"
    "tax-administration/pending-tax-foreclosures"
)
COUNTY = "Ashe"
MAX_PDF_AGE_DAYS = 365

# "13190 307 045 Crown Spruce Lane ..." / "07072 052 W. Calhoun Rd ..."
ROW_RE = re.compile(
    r"(?P<parcel>\d{5}(?:\s+\d{3})?(?:\s+\d{3})?)\s+"
    r"(?P<addr>.+?)\s+"
    r"(?P<status>TBA|Pending|In payment plan|Sale in Upset Bid period)\s+"
    r"(?P<sale>(?:TBA|\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}\s*[AP]M)?"
    r"|Call Clerk of Court[^\$]*?))\s*"
    r"(?P<bid>\$[\d,]+(?:\.\d{2})?|Call Clerk of Court[^\n]*)?",
    re.IGNORECASE,
)


def _extract_pdf_text(data: bytes) -> Optional[str]:
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber not installed; cannot parse Ashe PDF")
        return None
    try:
        import io
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        logger.warning("Ashe PDF parse failed: %s", e)
        return None


class AsheCountyScraper(BaseScraper):
    """Scrape the Ashe County pending-foreclosures PDF (freshness-guarded)."""

    SOURCE_NAME = "ashe_county"
    BASE_URL = PAGE_URL

    def __init__(self, delay_range: tuple[float, float] = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)

    def scrape(self) -> list[PropertyData]:
        logger.info("Checking Ashe County pending foreclosures ...")
        try:
            links = cs.find_links(PAGE_URL, pattern=r"\.pdf|pending.tax.foreclosure")
        except Exception as e:
            logger.error("Ashe County page fetch failed: %s", e)
            return []
        pdf_links = [l for l in links if l["href"].lower().endswith(".pdf")]
        if not pdf_links:
            cs.log_kept_empty(
                self.SOURCE_NAME, COUNTY, "no listing PDF linked", "", PAGE_URL,
            )
            return []
        pdf_url = pdf_links[0]["href"]
        logger.info("Ashe listing PDF: %s", pdf_url)
        try:
            data = cs.download_bytes(pdf_url)
        except Exception as e:
            logger.error("Ashe PDF download failed: %s", e)
            return []
        if not data:
            return []
        text = _extract_pdf_text(data)
        if not text:
            return []

        updated = cs.updated_date(text)
        if updated:
            try:
                age = (date.today() - date.fromisoformat(updated)).days
            except ValueError:
                age = None
            if age is not None and age > MAX_PDF_AGE_DAYS:
                cs.log_kept_empty(
                    self.SOURCE_NAME, COUNTY,
                    f"listing PDF stale (updated {updated}, {age}d old)",
                    text[:2000], pdf_url,
                )
                logger.info("Ashe County: PDF stale (%s), monitoring", updated)
                return []

        flat = re.sub(r"\s+", " ", text)
        properties: list[PropertyData] = []
        for m in ROW_RE.finditer(flat):
            parcel = re.sub(r"\s+", "", m.group("parcel"))
            addr = m.group("addr").strip(" .,")
            status = m.group("status").strip()
            sale_raw = m.group("sale").strip()
            bid_raw = (m.group("bid") or "").strip()
            price = cs.parse_money(bid_raw)
            sale_iso = cs.to_iso_date(sale_raw)
            lid = f"ashe_{parcel}_{cs.content_hash(addr + status)[:8]}"
            desc = f"[Ashe Co] parcel {parcel} {addr} — {status}"
            if price:
                desc += f", min bid ${price:,.2f}"
            log_raw(
                self.SOURCE_NAME, listing_id=lid, county=COUNTY, state="NC",
                decision="kept_tax", reason=f"pdf row status={status}",
                raw_text=m.group(0), url=pdf_url,
            )
            properties.append(cs.build_tax_row(
                source=self.SOURCE_NAME, listing_id=lid, url=pdf_url,
                county=COUNTY, parcel=parcel, address=addr, price=price,
                auction_date=sale_iso, court_case=None,
                description=desc, raw_text=m.group(0),
                extra_key=f"{status}|{updated or ''}",
            ))

        if not properties:
            cs.log_kept_empty(
                self.SOURCE_NAME, COUNTY, "PDF parsed, no parcel rows",
                text[:2000], pdf_url,
            )
        else:
            logger.info("Ashe County: %d parcel rows (PDF updated %s)",
                        len(properties), updated)
        return properties
