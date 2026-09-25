"""Shared helpers for NC county static-page scrapers (ashe/haywood/macon/mcdowell).

The four county tax-foreclosure pages are plain static HTML with no captcha
and no Turnstile gate — a camoufox page load plus DOM reads is all that is
needed. This module holds the common fetch / table / regex / row-builder
pieces so the per-county modules stay thin.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional

from .base import PropertyData, camoufox_context
from .rawlog import log_raw

logger = logging.getLogger(__name__)

# McDowell-style dash parcels (1739-00-31-2535) and explicitly labeled parcel
# IDs ("Parcel #12345", "PIN: 061878609600000"). Bare digit runs are
# deliberately NOT matched — they hit ZIP codes (28734), phone fragments,
# years and dollar fragments (see 2026-09-25 macon_28734 false positive).
PARCEL_RE = re.compile(
    r"\b\d{3,5}-\d{2}-\d{2,3}-\d{3,5}\b"      # 1739-00-31-2535
    r"|(?:parcel|pin|pid|reid)\s*[:#]?\s*(\d{4,}(?:[-\s]*\d+)*)",
    re.IGNORECASE,
)
# NC court case numbers: 26CV000538-580 (suffix optional).
CASE_RE = re.compile(r"\b(\d{2}\s?(?:SP|CVS|CVD|CVR|CV|E)\s?\d{3,6}(?:-\d{1,4})?)\b")
MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
LONG_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
UPDATED_RE = re.compile(
    r"updated\s*[:.]?\s*(\d{1,2}/\d{1,2}/\d{4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def find_parcels(text: str) -> list[str]:
    """Parcel IDs in *text*: dash-form parcels plus explicitly labeled IDs.

    Returns normalized digit/dash tokens (label prefixes stripped).
    """
    out: list[str] = []
    for m in PARCEL_RE.finditer(text or ""):
        token = m.group(1) or m.group(0)
        token = re.sub(r"\s+", "", token).strip("-")
        if token and token not in out:
            out.append(token)
    return out


def content_hash(text: str) -> str:
    """Short stable hash of normalized text for monitor listing ids."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:12]


def parse_money(text: str) -> Optional[float]:
    """First $ amount in *text* as a float, or None."""
    m = MONEY_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def find_case(text: str) -> Optional[str]:
    """First NC court case number in *text* (spaces stripped), or None."""
    m = CASE_RE.search(text or "")
    return m.group(1).replace(" ", "") if m else None


def to_iso_date(text: str) -> Optional[str]:
    """First MM/DD/YYYY or 'Month D, YYYY' date in *text* as ISO, or None."""
    m = MDY_RE.search(text or "")
    if m:
        try:
            return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        except ValueError:
            return None
    m = LONG_DATE_RE.search(text or "")
    if m:
        mon = _MONTHS.get(m.group(1)[:3].lower())
        if mon:
            try:
                return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"
            except ValueError:
                return None
    return None


def updated_date(text: str) -> Optional[str]:
    """'Updated: <date>' stamp in *text* as ISO, or None."""
    m = UPDATED_RE.search(text or "")
    return to_iso_date(m.group(1)) if m else None


def fetch_page(url: str, wait_ms: int = 2500, timeout: int = 60000) -> dict:
    """Load *url* in camoufox; return final/title/body_text/html/status."""
    out: dict = {"url": url, "ok": False}
    with camoufox_context() as page:
        page.set_viewport_size({"width": 1600, "height": 1000})
        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_timeout(wait_ms)
        out["final"] = page.url
        try:
            out["status"] = resp.status if resp else None
        except Exception:
            out["status"] = None
        try:
            out["title"] = page.title() or ""
        except Exception:
            out["title"] = ""
        try:
            out["body"] = page.inner_text("body") or ""
        except Exception:
            out["body"] = ""
        try:
            out["html"] = page.content() or ""
        except Exception:
            out["html"] = ""
        out["ok"] = True
    return out


def extract_tables(url: str, wait_ms: int = 2500) -> list[dict]:
    """Load *url* and return [{headers, rows}] for each HTML table found."""
    tables: list[dict] = []
    with camoufox_context() as page:
        page.set_viewport_size({"width": 1600, "height": 1000})
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        raw = page.evaluate("""() => Array.from(document.querySelectorAll('table')).map(t => {
            const head = Array.from(t.querySelectorAll('thead th')).map(h => (h.innerText||'').trim().replace(/\\s+/g,' '));
            const rows = Array.from(t.querySelectorAll('tbody tr, tr')).map(tr =>
                Array.from(tr.querySelectorAll('th,td')).map(c => (c.innerText||'').trim().replace(/\\s+/g,' ')));
            return {head, rows};
        })""") or []
    for t in raw:
        headers = t.get("head") or (t["rows"][0] if t.get("rows") else [])
        rows = t.get("rows") or []
        # Drop a header row duplicated in body rows.
        if rows and headers and rows[0] == headers:
            rows = rows[1:]
        tables.append({"headers": headers, "rows": [r for r in rows if any(r)]})
    return tables


def find_links(url: str, pattern: str = r"\.pdf|\.doc|\.xls|foreclosure|upset|sale|bid|parcel|gis",
               wait_ms: int = 2000) -> list[dict]:
    """Load *url* and return [{text, href}] links matching *pattern*."""
    rx = re.compile(pattern, re.IGNORECASE)
    with camoufox_context() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        links = page.evaluate("""() => Array.from(document.querySelectorAll('a'))
            .map(a => ({text: ((a.innerText||'').trim().replace(/\\s+/g,' ').slice(0,120)), href: a.href}))""") or []
    return [l for l in links if l.get("href") and rx.search(f"{l.get('text','')} {l.get('href','')}")]


def download_bytes(url: str, timeout: int = 60000) -> Optional[bytes]:
    """Download *url* through camoufox (inherits browser fetch, no plain HTTP)."""
    with camoufox_context() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_timeout(1500)
        resp = page.request.get(url, timeout=timeout)
        if not resp or not resp.ok:
            return None
        body = resp.body()
        return bytes(body) if body else None


def build_tax_row(
    *,
    source: str,
    listing_id: str,
    url: str,
    county: str,
    parcel: Optional[str] = None,
    address: Optional[str] = None,
    price: Optional[float] = None,
    auction_date: Optional[str] = None,
    upset_bid: Optional[str] = None,
    court_case: Optional[str] = None,
    description: Optional[str] = None,
    raw_text: Optional[str] = None,
    extra_key: str = "",
) -> PropertyData:
    """Build a tax_foreclosure PropertyData with NC map/GIS links."""
    from .nc_gis_lookup import build_gis_url, build_google_maps_url
    gis_url = build_gis_url(None, None, parcel, address, county, state="NC")
    maps_url = (
        build_google_maps_url(None, None, address, None, county, state="NC")
        if address else None
    )
    return PropertyData(
        source=source,
        source_listing_id=listing_id,
        url=url,
        address=address,
        city=None,
        county=county,
        state="NC",
        zip_code=None,
        latitude=None,
        longitude=None,
        price=price,
        acres=None,
        acres_source="placeholder",
        description=description,
        property_type="tax_foreclosure",
        court_case=court_case,
        auction_date=auction_date,
        upset_bid=upset_bid,
        parcel_number=parcel,
        raw_source_text=raw_text,
        gis_url=gis_url,
        google_maps_url=maps_url,
        foreclosure_key=f"{source}|{listing_id}|{parcel or ''}|{extra_key}",
    )


def log_kept_empty(source: str, county: str, reason: str, raw_text: str, url: str) -> None:
    """Standard kept_empty rawlog for monitor scrapers with nothing to report."""
    log_raw(
        source, listing_id="monitor", county=county, state="NC",
        decision="kept_empty", reason=reason,
        raw_text=(raw_text or "")[:2000], url=url,
    )
