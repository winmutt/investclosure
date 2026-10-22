"""GovEase tax-sale auction schedule scraper: row parsing + REAP signals."""
from __future__ import annotations

import json

import pytest

from scraper.govease import (
    GovEaseScraper,
    _clean_city,
    _mdy_to_iso,
    _normalize_county,
    _parse_liveauctions_url,
)


def _row(*texts, hrefs=None):
    cells = []
    for i, t in enumerate(texts):
        cells.append({"t": t,
                      "hrefs": hrefs if (i == 8 and hrefs) else []})
    return cells


SULLIVAN_JUDICIAL = _row(
    "Sullivan", "TN", "9/29/2026", "11/10/2026", "8:30:00 AM",
    "11/10/2026", "10:00:00 AM", "Judicial Sale",
    "Sullivan Parcel List", "location_on\nBlountville",
    hrefs=["https://liveauctions.govease.com/tn/tnsullivan/1432/auctions/refresh"],
)


class TestHelpers:
    def test_mdy_to_iso(self):
        assert _mdy_to_iso("11/10/2026") == "2026-11-10"
        assert _mdy_to_iso("11/03/2026") == "2026-11-03"  # zero-padded
        assert _mdy_to_iso("4/17/2026") == "2026-04-17"
        assert _mdy_to_iso("") is None
        assert _mdy_to_iso(None) is None

    def test_clean_city(self):
        assert _clean_city("location_on\nBlountville") == "Blountville"
        assert _clean_city("location_on\nKnoxville") == "Knoxville"
        assert _clean_city("") is None

    def test_parse_liveauctions_url(self):
        slug, aid = _parse_liveauctions_url(
            "https://liveauctions.govease.com/tn/tnsullivan/1432/auctions/refresh",
            "Sullivan")
        assert (slug, aid) == ("tnsullivan", "1432")
        # no link -> synthesized from county label
        assert _parse_liveauctions_url("", "Hawkins") == ("hawkins", "0")

    def test_normalize_county(self):
        assert _normalize_county("Sullivan") == "Sullivan"
        assert _normalize_county("Hawkins") == "Hawkins"
        assert _normalize_county("Gibson County - Trenton Chancery") is None
        assert _normalize_county("Madison") is None  # not in target set
        assert _normalize_county("Weakley") is None


class TestRowParse:
    def test_sullivan_judicial_row(self):
        s = GovEaseScraper()
        prop = s._parse_row(SULLIVAN_JUDICIAL)
        assert prop is not None
        assert prop["source"] == "govease"
        assert prop["source_listing_id"] == "tnsullivan-1432-judicial-sale"
        assert prop["county"] == "Sullivan"
        assert prop["state"] == "TN"
        assert prop["property_type"] == "tax_foreclosure"
        assert prop["auction_date"] == "2026-11-10"
        assert prop["city"] == "Blountville"
        assert "liveauctions.govease.com" in prop["url"]
        assert "Judicial Sale" in prop["description"]
        # address-less: no maps/gis links (link-depth policy)
        assert prop["google_maps_url"] is None
        assert prop["gis_url"] is None

    def test_non_target_county_dropped(self):
        weakley = _row(
            "Weakley", "TN", "9/8/2026", "10/2/2026", "10:00:00 AM",
            "10/6/2026", "10:00:00 AM", "Redeemable Deed Sale",
            "Weakley Parcel List", "location_on\nDresden",
            hrefs=["https://liveauctions.govease.com/tn/tnweakley/1205/auctions/refresh"])
        assert GovEaseScraper()._parse_row(weakley) is None

    def test_surplus_sale_dropped(self):
        row = _row(
            "Madison County Board of Commissioners", "TN", "10/18/2026",
            "10/13/2026", "10:00:00 AM", "10/16/2026", "10:00:00 AM",
            "Surplus Sale", "Madison Parcel List", "location_on\nJackson",
            hrefs=["https://liveauctions.govease.com/tn/tnmadisonsurplus/1421/auctions/refresh"])
        assert GovEaseScraper()._parse_row(row) is None

    def test_foreign_state_dropped(self):
        row = _row("Washington", "CO", "10/5/2026", "11/03/2026", "11:00:00 AM",
                   "11/04/2026", "11:00:00 AM", "Lien Sale", "", "location_on\nAkron")
        assert GovEaseScraper()._parse_row(row) is None


class TestReapSignals:
    def test_finalize_observed_collects_target_ids(self):
        rows = [SULLIVAN_JUDICIAL]
        s = GovEaseScraper()
        s._finalize_observed(rows)
        assert s.observed_listing_ids == {"tnsullivan-1432-judicial-sale"}

    def test_reap_registration(self):
        from scraper.db import EMPTY_REAP_ALLOWED, REAP_SOURCES
        assert "govease" in REAP_SOURCES
        assert "blount_tax" in EMPTY_REAP_ALLOWED
