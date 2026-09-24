"""Swain County (NC) foreclosure-listing monitor.

Swain County posts tax foreclosure listings on its Tag Office page:

    https://www.swaincountync.gov/tag-office/

as an Elementor toggle titled "FORECLOSURE LISTINGS"
(``#elementor-tab-content-4992``). When no sale is pending the toggle
reads "None at this time." — this scraper watches that toggle and emits
a single alert row the moment its content changes to anything else, so
the normal pipeline (DB insert + Telegram per-property alert) fires once
per distinct listing text. Repeat runs with unchanged text dedup
silently via a content-hashed ``source_listing_id``.

No captcha, no county filtering (single county), no parcel parsing — the
county page carries no per-parcel data, only the presence/absence signal.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

from .base import BaseScraper, PropertyData, camoufox_context
from .rawlog import log_raw

logger = logging.getLogger(__name__)

TAG_OFFICE_URL = "https://www.swaincountync.gov/tag-office/"
TOGGLE_SELECTOR = "#elementor-tab-content-4992"

# Normalized toggle texts that mean "no sale pending".
EMPTY_MARKERS = frozenset({
    "none at this time",
    "no listings at this time",
    "no current listings",
    "no foreclosures at this time",
    "no foreclosure listings at this time",
    "check back later",
})


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip trailing periods."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    return cleaned.rstrip(".").strip()


def _is_empty_listing(toggle_text: str) -> bool:
    """True when the toggle carries the county's 'nothing pending' marker."""
    cleaned = _normalize(toggle_text)
    if not cleaned:
        return True
    return cleaned in EMPTY_MARKERS or cleaned.startswith("none at this time")


def _content_hash(toggle_text: str) -> str:
    """Short stable hash of the toggle content for listing ids / keys."""
    return hashlib.sha1(_normalize(toggle_text).encode("utf-8")).hexdigest()[:12]


class SwainCountyScraper(BaseScraper):
    """Monitor for the Swain County Tag Office FORECLOSURE LISTINGS toggle."""

    SOURCE_NAME = "swain_county"
    BASE_URL = TAG_OFFICE_URL

    def __init__(self, delay_range: tuple[float, float] = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)

    def scrape(self) -> list[PropertyData]:
        logger.info("Checking Swain County foreclosure toggle ...")
        try:
            with camoufox_context() as page:
                page.goto(TAG_OFFICE_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1500)
                toggle_text, toggle_links = self._read_toggle(page)
        except Exception as e:
            logger.error("Swain County toggle fetch failed: %s", e)
            return []

        if toggle_text is None:
            logger.warning("Swain County toggle not found on page — skipping run")
            log_raw(
                self.SOURCE_NAME, listing_id="toggle", county="Swain",
                state="NC", decision="dropped_no_key",
                reason="toggle element missing", raw_text="",
                url=TAG_OFFICE_URL,
            )
            return []

        if _is_empty_listing(toggle_text):
            logger.info("Swain County: no listings (%r)", toggle_text.strip()[:60])
            log_raw(
                self.SOURCE_NAME, listing_id="toggle", county="Swain",
                state="NC", decision="kept_empty",
                reason="toggle reads none-at-this-time", raw_text=toggle_text,
                url=TAG_OFFICE_URL,
            )
            return []

        digest = _content_hash(toggle_text)
        logger.warning("Swain County toggle CHANGED (hash %s): %r",
                       digest, toggle_text.strip()[:120])
        log_raw(
            self.SOURCE_NAME, listing_id=f"swain_toggle_{digest}",
            county="Swain", state="NC", decision="kept_tax",
            reason="toggle no longer empty", raw_text=toggle_text,
            url=TAG_OFFICE_URL,
        )
        prop = self._build_alert(toggle_text, toggle_links, digest)
        return [prop]

    def _read_toggle(self, page) -> tuple[Optional[str], list[dict]]:
        """Return (toggle text, toggle links) or (None, []) if missing."""
        toggle = page.query_selector(TOGGLE_SELECTOR)
        if toggle is None:
            # Fallback: find the heading, then its sibling toggle content.
            heading = page.query_selector(":text('FORECLOSURE LISTINGS')")
            if heading is not None:
                try:
                    toggle = heading.evaluate_handle(
                        "el => el.closest('.elementor-toggle-item')"
                        "?.query_selector('.elementor-tab-content')"
                    ).as_element()
                except Exception:
                    toggle = None
        if toggle is None:
            return None, []
        try:
            text = toggle.inner_text() or ""
        except Exception:
            text = ""
        links: list[dict] = []
        try:
            links = toggle.eval_on_selector_all(
                "a", "els => els.map(e => ({t: (e.innerText||'').trim().slice(0,200), h: e.href}))"
            ) or []
        except Exception:
            links = []
        return text, links

    def _build_alert(self, toggle_text: str, links: list[dict], digest: str) -> PropertyData:
        text = (toggle_text or "").strip()
        desc_links = " | ".join(
            f"{l.get('t') or 'link'}: {l.get('h')}" for l in links if l.get("h")
        )
        description = f"Swain County FORECLOSURE LISTINGS changed: {text[:500]}"
        if desc_links:
            description += f" | Links: {desc_links[:500]}"
        prop: PropertyData = {
            "source": self.SOURCE_NAME,
            "source_listing_id": f"swain_toggle_{digest}",
            "url": TAG_OFFICE_URL,
            "address": None,
            "city": None,
            "county": "Swain",
            "state": "NC",
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": None,
            "acres": None,
            "acres_source": "placeholder",
            "description": description,
            "property_type": "tax_foreclosure",
            "image_url": None,
            "parcel_number": None,
            "gis_url": None,
            "auction_date": None,
            "close_date": None,
            "upset_bid": None,
            "foreclosure_key": f"swain_toggle_{digest}",
            "court_case": None,
            "initial_auction_date": None,
            "upset_bid_end": None,
            "google_maps_url": None,
            "google_maps_topo_url": None,
            "raw_source_text": f"{TAG_OFFICE_URL}\n\n{text}",
        }
        return prop
