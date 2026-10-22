"""Blount County TN tax-sale scraper (tn_county_tax): OCR text parsers."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from scraper.tn_county_tax import (
    _build_rows,
    _parse_property_blocks,
    _parse_sale_header,
    _stamp_is_stale,
)

# Fixture modeled on the live OCR output for Sale 33 (June 4 2026);
# page follows this notice.
SAMPLE_TEXT = """
Tax Sale: 33
Date of Sale: June 4, 2026
Time: 10:01 a.m.
Location: Heritage High School
Publication Dates in The Daily Times:
May 8, 2026
IN THE CHANCERY COURT FOR BLOUNT COUNTY, TENNESSEE
THE STATE OF TENNESSEE IN ITS
vs. No. 2025-CH-44
THE 2023 DELINQUENT PROPERTY
TAX OWNERS,
NOTICE OF PUBLICATION

In obedience to a decree of the Chancery Court at Maryville, I will on June
4, 2026, at 10:01 a.m., in Heritage High School, 3741 E. Lamar Alexander
Pkwy., Maryville, Tennessee, sell to the highest and best bidder the
property described in said decree, as follows:

1. Property described on Property Assessor's Map for Blount County,
Tennessee, as Parcel No. 06-077N-A-077N-056.00 located in Blount County,
Tennessee, assessed to Larry F. Adams, for 2023 County taxes, together
with interest, penalty, fees and costs in the total amount of $865.02.
For concise description, see deed of record in Deed Book 631, page 798.

2. Property described on Property Assessor's Map for Blount County,
Tennessee, as Parcel No. 043-001.02 located in Blount County, Tennessee,
assessed to Deerfield Estates LLC, now Deerfield Estates LLC Boone, for
2023 County taxes, together with interest, penalty, fees and costs in the
total amount of $1,204.55.
"""


class TestHeader:
    def test_sale_header_fields(self):
        header = _parse_sale_header(SAMPLE_TEXT)
        assert header["sale_no"] == "33"
        assert header["sale_date"] == "2026-06-04"
        assert header["court_case"] == "2025-CH-44"
        assert header["location"] == "Heritage High School"

    def test_missing_header_fields(self):
        out = _parse_sale_header("nothing matches here")
        assert out == {}


class TestBlocks:
    def test_two_parcels_parsed(self):
        blocks = _parse_property_blocks(SAMPLE_TEXT)
        assert len(blocks) == 2
        assert blocks[0]["parcel"] == "06-077N-A-077N-056.00"
        assert blocks[0]["owner"] == "Larry F. Adams"
        assert blocks[0]["amount"] == 865.02
        assert blocks[1]["parcel"] == "043-001.02"
        assert blocks[1]["amount"] == 1204.55
        # case no lives on the notice head, not inside numbered blocks
        assert blocks[0]["court_case"] is None
        assert blocks[1]["court_case"] is None

    def test_ocr_garbage_block_skipped(self):
        text = ("3. Property described on Property Assessor's Map as Parcel "
                "No. nfoscannn located Blount Tennessee, assessed...")
        assert _parse_property_blocks(text) == []


class TestRows:
    def test_build_rows(self):
        header = _parse_sale_header(SAMPLE_TEXT)
        blocks = _parse_property_blocks(SAMPLE_TEXT)
        rows = _build_rows("blount_tax", header, blocks,
                           "https://www.blounttn.gov/2033/Properties-for-Sale")
        assert len(rows) == 2
        r = rows[0]
        assert r["source"] == "blount_tax"
        assert r["source_listing_id"] == "sale33-06-077n-a-077n-056-00"
        assert r["county"] == "Blount"
        assert r["state"] == "TN"
        assert r["property_type"] == "tax_foreclosure"
        assert r["auction_date"] == "2026-06-04"
        assert r["parcel_number"] == "06-077N-A-077N-056.00"
        assert r["price"] == 865.02
        assert "Larry F. Adams" in r["description"]
        # no address -> no maps/gis links (link-depth policy)
        assert r["google_maps_url"] is None
        assert r["gis_url"] is None


class TestStaleGate:
    def test_stamp_stale(self):
        # June 2026 list seen in October 2026 -> >90 days -> stale
        assert _stamp_is_stale("June 03, 2026") is True
        # a fresh stamp (recently updated) -> not stale
        today = date.today()
        fresh = today.strftime("%B %d")
        fresh = fresh + f", {today.year}"
        assert _stamp_is_stale(fresh) is False
        assert _stamp_is_stale("garbage") is False


class TestBlountReap:
    def test_empty_reap_allowed_archives_previous_sale(self, tmp_path):
        from scraper.db import _ensure_db, insert_property, reap_missing_listings, archive_property

        conn = _ensure_db(tmp_path / "blount.db")
        _, row = insert_property(
            conn, source="blount_tax", source_listing_id="sale33-p1",
            url="https://www.blounttn.gov/2033/Properties-for-Sale",
            address=None, city=None, county="Blount", state="TN",
            zip_code=None, latitude=None, longitude=None, price_cents=0,
            property_type="tax_foreclosure")
        assert conn.execute(
            "SELECT status FROM properties WHERE id=?", (row["id"],)).fetchone()[0] == "active"

        # Successful scrape observing NOTHING (sale realized) archives it
        # because blount_tax is EMPTY_REAP_ALLOWED.
        reaped = reap_missing_listings(conn, "blount_tax", seen_ids=set(),
                                       observed_listing_ids=set())
        assert reaped == 1
        assert conn.execute(
            "SELECT status FROM properties WHERE id=?", (row["id"],)).fetchone()[0] == "archived"

    def test_reap_not_triggered_on_outage_signal(self, tmp_path):
        from scraper.db import _ensure_db, insert_property, reap_missing_listings

        conn = _ensure_db(tmp_path / "blount2.db")
        _, row = insert_property(
            conn, source="blount_tax", source_listing_id="sale33-p2",
            url="u", address=None, city=None, county="Blount", state="TN",
            zip_code=None, latitude=None, longitude=None, price_cents=0)
        # None observation = outage -> protected
        reaped = reap_missing_listings(conn, "blount_tax", seen_ids=set(),
                                       observed_listing_ids=None)
        assert reaped == 0
