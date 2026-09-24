"""Tests for the Swain County foreclosure-toggle monitor (network-free)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.swain_county import (
    SwainCountyScraper,
    _content_hash,
    _is_empty_listing,
)


def test_empty_markers_detected():
    assert _is_empty_listing("None at this time.")
    assert _is_empty_listing("  none at this time  ")
    assert _is_empty_listing("NONE AT THIS TIME.")
    assert _is_empty_listing("")
    assert _is_empty_listing("   ")


def test_nonempty_toggle_detected():
    assert not _is_empty_listing("Sale scheduled October 7th — see attached list")
    assert not _is_empty_listing("3 parcels listed below")
    # Prefix rule: county keeps the "None at this time." lead-in when appending
    # listings, so a leading marker still counts as empty by design.
    assert _is_empty_listing("None at this time. 123 Main St, Bryson City")


def test_content_hash_stable_and_distinct():
    assert _content_hash("None at this time.") == _content_hash("none at this time. ")
    assert _content_hash("Sale Oct 7") != _content_hash("Sale Nov 4")


def test_alert_row_shape():
    scraper = SwainCountyScraper()
    prop = scraper._build_alert("Sale Oct 7: 123 Main St", [], "abc123")
    assert prop["source"] == "swain_county"
    assert prop["county"] == "Swain"
    assert prop["state"] == "NC"
    assert prop["property_type"] == "tax_foreclosure"
    assert prop["source_listing_id"] == "swain_toggle_abc123"
    assert "123 Main St" in prop["description"]
    assert prop["url"] == "https://www.swaincountync.gov/tag-office/"
