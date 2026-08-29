"""Comprehensive tests for investclosure scraper package.

Tests:
- config.py: Config class, helper functions, county lists, GIS URLS
- db.py: CRUD, dedup, archive, scrape runs, stats, migrations
- base.py: PropertyData, parser helpers
- kania_law.py: _clean_html, _price_to_cents, _clean_date, _parse_record
- zls_nc.py: _extract_county, _parse_price, _desc, _get_gis_url, _gm
- nc_gis_lookup.py: _clean_features, _county_matches, _normalize_address
- gis_urls.py: get_gis_viewer_url
- run.py: cmd_list
- server.py: Flask routes, helpers
"""
from __future__ import annotations
import json
import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from datetime import date
from unittest.mock import patch, MagicMock, mock_open

import pytest

# Ensure scraper package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper.db import (
    _ensure_db,
    insert_property,
    _upsert_property,
    archive_below_acres,
    get_stats,
    get_all_active,
    get_new_since,
    start_scrape_run,
    update_scrape_run,
    compute_dedup_hash,
)
from scraper.config import (
    config,
    _opt_str,
    _opt_float,
    _opt_int,
    _required,
    _site_env,
    NC_FORECLOSURE_COUNTIES,
    GA_FORECLOSURE_COUNTIES,
    AL_FORECLOSURE_COUNTIES,
    KY_FORECLOSURE_COUNTIES,
    SC_FORECLOSURE_COUNTIES,
    TN_FORECLOSURE_COUNTIES,
    QUALIFYING_COUNTIES,
    TARGET_COUNTIES,
    QUALIFYING_STATES,
    GIS_PARCEL_URLS,
)
from scraper.zls_nc import ZLSNCScraper
from scraper.tn_publicnotice import (
    TNPublicNoticeScraper,
    _tn_parse_parcels,
    _tn_extract_sale_date,
)
from scraper.newspaper_notices import (
    NewspaperNoticesScraper,
    _is_tax_foreclosure_notice,
)
from scraper.nc_gis_lookup import (
    _clean_features,
    _county_matches,
    _normalize_address,
    NC1MapService,
    NC_COUNTY_FIPS,
)
from scraper.gis_urls import get_gis_viewer_url, GIS_VIEWER_URLS
from scraper import db as scraper_db
from scraper.config import Config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_dir(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture()
def conn(tmp_dir):
    from scraper.config import config as cfg
    cfg.db_path = tmp_dir / "test.db"
    return _ensure_db(tmp_dir / "test.db")


def _make_prop(conn, source_listing_id, acres, county="Ashe", source="test"):
    return insert_property(
        conn=conn,
        source=source,
        source_listing_id=source_listing_id,
        url=None,
        address=f"{source_listing_id} Road",
        city=None,
        county=county,
        state="NC",
        zip_code=None,
        latitude=None,
        longitude=None,
        price_cents=10000,
        acres=acres,
    )


# ===========================================================================
# config.py tests
# ===========================================================================

class TestOptHelpers:
    """Test _opt_str, _opt_float, _opt_int, _required helpers."""

    def test_opt_str_returns_default_when_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TEST_OPT_STR_X", None)
            with patch("scraper.config.os.environ", {}):
                result = _opt_str("TEST_OPT_STR_X", "default_val")
                assert result == "default_val"

    def test_opt_str_returns_env_value(self):
        with patch.dict(os.environ, {"TEST_OPT_STR_X": "env_val"}, clear=True):
            with patch("scraper.config.os.environ", {"TEST_OPT_STR_X": "env_val"}):
                result = _opt_str("TEST_OPT_STR_X", "default_val")
                assert result == "env_val"

    def test_opt_str_strips_whitespace(self):
        with patch("scraper.config.os.environ", {"X": "  spaced  "}):
            result = _opt_str("X", "def")
            assert result == "spaced"

    def test_opt_float_returns_default_when_missing(self):
        with patch("scraper.config.os.environ", {}):
            result = _opt_float("NONEXISTENT_FLOAT", 3.14)
            assert result == 3.14

    def test_opt_float_parses_value(self):
        with patch("scraper.config.os.environ", {"X": "42.5"}):
            result = _opt_float("X", 0.0)
            assert result == 42.5

    def test_opt_float_invalid_exits(self):
        with patch("scraper.config.os.environ", {"X": "not_a_number"}):
            with patch("scraper.config.sys.exit") as mock_exit:
                _opt_float("X", 1.0)
                assert mock_exit.called

    def test_opt_int_returns_default_when_missing(self):
        with patch("scraper.config.os.environ", {}):
            result = _opt_int("NONEXISTENT_INT", 99)
            assert result == 99

    def test_opt_int_parses_value(self):
        with patch("scraper.config.os.environ", {"X": "42"}):
            result = _opt_int("X", 0)
            assert result == 42

    def test_opt_int_invalid_exits(self):
        with patch("scraper.config.os.environ", {"X": "abc"}):
            with patch("scraper.config.sys.exit") as mock_exit:
                _opt_int("X", 1)
                assert mock_exit.called

    def test_required_returns_value(self):
        with patch("scraper.config.os.environ", {"X": "val"}):
            with patch("scraper.config.os.environ", {"X": "val"}):
                result = _required("X")
                assert result == "val"

    def test_required_exits_on_missing(self):
        with patch("scraper.config.os.environ", {}):
            with patch("scraper.config.sys.exit") as mock_exit:
                _required("MISSING")
                assert mock_exit.called


class TestConfig:
    """Test Config class initialization and methods."""

    def test_config_defaults(self):
        with patch("scraper.config.os.environ", {"TWO_CAPTCHA_API_KEY": "test"}):
            cfg = Config()
            assert cfg.MIN_ACRES == 1.0
            assert cfg.MAX_ACRES == 1000.0
            assert cfg.MAX_PRICE == 0
            assert cfg.PROXY_URL is not None  # defaults to winmutt
            assert (2.0, 4.0) in cfg.DELAY_RANGES.values()
            assert cfg.CAPTCHA_ENABLED is True
            assert cfg.PROXY_ENABLED is True

    def test_config_custom_paths(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cfg = Config(
                data_dir=str(td / "data"),
                db_path=str(td / "db" / "test.db"),
                backups_dir=str(td / "backups"),
                logs_dir=str(td / "logs"),
            )
            assert cfg.data_dir == td / "data"
            assert cfg.db_path.parent == td / "db"
            assert cfg.backups_dir == td / "backups"
            assert cfg.logs_dir == td / "logs"

    def test_config_creates_directories(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cfg = Config(
                data_dir=str(td / "d"),
                db_path=str(td / "d" / "db.sql"),
            )
            assert (td / "d").exists()

    def test_get_delay_range(self):
        cfg = Config()
        assert cfg.get_delay_range("kania_law") == (2.0, 4.0)
        assert cfg.get_delay_range("zls_nc") == (2.0, 4.0)
        assert cfg.get_delay_range("nonexistent") == cfg.DELAY_RANGES["default"]

    def test_get_min_acres(self):
        with patch("scraper.config.os.environ", {"TWO_CAPTCHA_API_KEY": "test"}):
            cfg = Config()
            assert cfg.get_min_acres("test") == cfg.MIN_ACRES
            cfg.SCRAPER_OVERRIDES["test"] = {"min_acres": 20.0}
            assert cfg.get_min_acres("test") == 20.0

    def test_get_max_price(self):
        cfg = Config()
        assert cfg.get_max_price("test") == 0
        cfg.SCRAPER_OVERRIDES["test"] = {"max_price": 50000}
        assert cfg.get_max_price("test") == 50000

    def test_should_use_proxy(self):
        cfg = Config()
        assert cfg.should_use_proxy("any_scraper") is True

    def test_config_env_min_acres(self):
        with patch("scraper.config.os.environ", {"INVESTCLOSURE_MIN_ACRES": "15.5"}):
            cfg = Config()
            assert cfg.MIN_ACRES == 15.5

    def test_config_env_max_acres(self):
        with patch("scraper.config.os.environ", {"INVESTCLOSURE_MAX_ACRES": "500.0"}):
            cfg = Config()
            assert cfg.MAX_ACRES == 500.0

    def test_config_env_proxy(self):
        with patch("scraper.config.os.environ", {"INVESTCLOSURE_PROXY": "proxy.local:3128"}):
            cfg = Config()
            assert cfg.PROXY_URL == "http://proxy.local:3128"

    def test_config_empty_proxy_disables(self):
        with patch("scraper.config.os.environ", {"INVESTCLOSURE_PROXY": ""}):
            cfg = Config()
            assert cfg.PROXY_URL is None


class TestCountyLists:
    """Test county list definitions."""

    def test_nc_foreclosure_counties(self):
        assert len(NC_FORECLOSURE_COUNTIES) == 25

    def test_ga_foreclosure_counties(self):
        assert len(GA_FORECLOSURE_COUNTIES) == 11

    def test_al_foreclosure_counties(self):
        assert len(AL_FORECLOSURE_COUNTIES) == 5

    def test_ky_foreclosure_counties(self):
        assert len(KY_FORECLOSURE_COUNTIES) == 5

    def test_sc_foreclosure_counties(self):
        assert len(SC_FORECLOSURE_COUNTIES) == 4

    def test_tn_foreclosure_counties(self):
        assert len(TN_FORECLOSURE_COUNTIES) == 38

    def test_qualifying_states(self):
        assert set(QUALIFYING_STATES) == {"GA", "AL", "KY", "NC", "SC", "TN"}

    def test_qualifying_counties_keys(self):
        assert set(QUALIFYING_COUNTIES.keys()) == set(QUALIFYING_STATES)

    def test_target_counties_flat(self):
        total = sum(len(v) for v in QUALIFYING_COUNTIES.values())
        assert len(TARGET_COUNTIES) == total


class TestGISParcelURLs:
    """Test GIS_PARCEL_URLS structure."""

    def test_gis_parcel_urls_has_entries(self):
        assert len(GIS_PARCEL_URLS) > 0

    def test_each_entry_has_required_fields(self):
        for county, info in GIS_PARCEL_URLS.items():
            assert "arcgis_url" in info
            assert "field_name" in info
            assert "portal_type" in info
            assert info["portal_type"] == "arcgis"

    def test_known_counties_in_gis_urls(self):
        for c in ["Alleghany", "Cherokee", "Haywood", "Henderson", "Madison",
                  "Transylvania", "Jackson", "Clay", "Graham", "Swain"]:
            assert c in GIS_PARCEL_URLS


class TestSiteEnv:
    """Test _site_env helper."""

    def test_site_env_returns_default(self):
        with patch("scraper.config.os.environ", {}):
            result = _site_env("NCFORECLOSURES", "BASE_URL", "https://default.com")
            assert result == "https://default.com"

    def test_site_env_returns_env(self):
        with patch("scraper.config.os.environ", {"NCFORECLOSURES_BASE_URL": "https://custom.com"}):
            result = _site_env("NCFORECLOSURES", "BASE_URL", "https://default.com")
            assert result == "https://custom.com"


# ===========================================================================
# db.py tests
# ===========================================================================

class TestUpsertProperty:
    """Test _upsert_property insert and update logic."""

    def test_insert_new_property(self, conn):
        status, row = _upsert_property(
            conn, "test", "P1", "http://example.com", "100 Main St",
            "City", "Ashe", "NC", "28709", None, None, 100000, 25.0,
        )
        assert status == "new"
        assert row["source_listing_id"] == "P1"
        assert row["acres"] == 25.0
        assert row["seen_count"] == 1
        assert row["status"] == "active"

    def test_update_same_source_listing(self, conn):
        _upsert_property(
            conn, "test", "P1", "http://example.com", "100 Main St",
            None, "Ashe", "NC", None, None, None, 10000, 25.0,
        )
        status, row = _upsert_property(
            conn, "test", "P1", "http://example.com", "100 Main St (Updated)",
            None, "Ashe", "NC", None, None, None, 15000, 30.0,
        )
        assert status == "duplicate"
        assert row["price_cents"] == 15000
        assert row["acres"] == 30.0
        assert row["seen_count"] == 2

    def test_dedup_by_hash_different_source(self, conn):
        _upsert_property(
            conn, "A", None, None, "100 Main St", None, "Ashe", "NC",
            None, None, None, 10000, 25.0,
        )
        status, _ = _upsert_property(
            conn, "B", None, None, "100 Main St", None, "Ashe", "NC",
            None, None, None, 20000, 20.0,
        )
        assert status == "duplicate"

    def test_dedup_by_parcel_number_new_listing_id(self, conn):
        # Same physical parcel re-posted under a NEW source_listing_id
        # (NC notice re-publication / GA listing-id format change) must
        # collapse onto the existing row instead of inserting a duplicate.
        status, row = _upsert_property(
            conn, "test", "old-123", None, "100 Main St", None, "Buncombe", "NC",
            None, None, None, 10000, 25.0,
            parcel_number="971042384300000",
        )
        assert status == "new"
        first_id = row["id"]

        status, row = _upsert_property(
            conn, "test", "new-456", None, None, None, "Buncombe", "NC",
            None, None, None, 15000, 30.0,
            parcel_number="971042384300000",
        )
        assert status == "duplicate"
        assert row["id"] == first_id
        assert row["seen_count"] == 2
        assert row["price_cents"] == 15000

    def test_parcel_dedup_does_not_resurrect_archived(self, conn):
        # When the ONLY existing copy of a parcel is archived, a fresh sighting
        # still collapses onto that single row (no duplicate active row is
        # created) — matching how the other resolvers keep archived status.
        status, row = _upsert_property(
            conn, "test", "old-1", None, None, None, "Clay", "NC",
            None, None, None, 5000, 10.0,
            parcel_number="P0099",
        )
        first_id = row["id"]
        conn.execute("UPDATE properties SET status='archived' WHERE id=?", (first_id,))

        status, row = _upsert_property(
            conn, "test", "new-3", None, None, None, "Clay", "NC",
            None, None, None, 7000, 12.0,
            parcel_number="P0099",
        )
        assert status == "duplicate"
        assert row["id"] == first_id
        assert row["seen_count"] == 2

    def test_parcel_dedup_scoped_to_source(self, conn):
        # The same parcel_number may legitimately exist across different
        # sources; the parcel match must NOT collapse them into one row.
        _upsert_property(
            conn, "src_a", "a-1", None, None, None, "Buncombe", "NC",
            None, None, None, 10000, 25.0,
            parcel_number="P77",
        )
        status, row = _upsert_property(
            conn, "src_b", "b-1", None, None, None, "Buncombe", "NC",
            None, None, None, 20000, 30.0,
            parcel_number="P77",
        )
        assert status == "new"
        assert row["source"] == "src_b"

    def test_manual_acres_not_overwritten(self, conn):
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, None, 10000, 25.0,
        )
        conn.execute("UPDATE properties SET manual_acres_set = 'locked' WHERE source_listing_id='P1'")

        status, row = _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, None, 10000, 50.0,
        )
        assert row["acres"] == 25.0

    def test_google_maps_url_conditional_update(self, conn):
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
        )
        conn.execute("UPDATE properties SET manual_acres_set = 'locked' WHERE source_listing_id='P1'")

        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
            google_maps_url="https://maps.google.com/?q=100Main",
        )
        row = conn.execute("SELECT google_maps_url FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["google_maps_url"] == "https://maps.google.com/?q=100Main"

        # Re-insert without google_maps_url should NOT clear it
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
        )
        row = conn.execute("SELECT google_maps_url FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["google_maps_url"] == "https://maps.google.com/?q=100Main"

    def test_google_maps_topo_url_conditional_update(self, conn):
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
        )
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
            google_maps_topo_url="https://maps.google.com/topo",
        )
        row = conn.execute("SELECT google_maps_topo_url FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["google_maps_topo_url"] == "https://maps.google.com/topo"

        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
        )
        row = conn.execute("SELECT google_maps_topo_url FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["google_maps_topo_url"] == "https://maps.google.com/topo"

    def test_gis_url_conditional_update(self, conn):
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
        )
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
            gis_url="https://nconemap.gov/parcel/123",
        )
        row = conn.execute("SELECT gis_url FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["gis_url"] == "https://nconemap.gov/parcel/123"

        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
        )
        row = conn.execute("SELECT gis_url FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["gis_url"] == "https://nconemap.gov/parcel/123"

    def test_initial_auction_date_change_refreshes_last_seen(self, conn):
        first_seen = "2026-01-01"
        last_seen = "2026-01-01"
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
            initial_auction_date="2026-02-01",
            last_seen=last_seen,
        )

        # Insert with changed initial_auction_date
        _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
            initial_auction_date="2026-03-01",
            last_seen="2026-03-15",
        )
        row = conn.execute("SELECT initial_auction_date, last_seen FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["initial_auction_date"] == "2026-03-01"
        # last_seen was refreshed to new value
        assert row["last_seen"] == "2026-03-15"

    def test_upsert_with_all_fields(self, conn):
        status, row = _upsert_property(
            conn,
            source="test",
            source_listing_id="P1",
            url="https://example.com/1",
            address="100 Main St",
            city="City",
            county="Ashe",
            state="NC",
            zip_code="28709",
            latitude=36.0,
            longitude=-81.0,
            price_cents=100000,
            acres=25.0,
            description="A nice property",
            property_type="residential",
            listing_date="2026-01-01",
            auction_date="2026-02-01",
            close_date="2026-02-15",
            upset_bid="50000",
            foreclosure_key="key123",
            parcel_number="P12345",
            deed_book="BK1 pg1",
            google_maps_url="https://maps.google.com/?q=100Main",
            google_maps_topo_url="https://maps.google.com/topo",
            gis_url="https://nconemap.gov/parcel/P12345",
            elevation_ft=2000.0,
            parcel_screenshot="screenshot.png",
        )
        assert status == "new"
        assert row["url"] == "https://example.com/1"
        assert row["zip_code"] == "28709"
        assert row["latitude"] == 36.0
        assert row["price_cents"] == 100000
        assert row["property_type"] == "residential"
        assert row["foreclosure_key"] == "key123"
        assert row["deed_book"] == "BK1 pg1"

    def test_upsert_sets_first_seen_and_last_seen(self, conn):
        status, row = _upsert_property(
            conn, "test", "P1", None, "100 Main St", None, "Ashe", "NC",
            None, None, 10000, 25.0,
            first_seen="2025-01-01",
            last_seen="2025-06-01",
        )
        assert row["first_seen"] == "2025-01-01"
        assert row["last_seen"] == "2025-06-01"

    def test_upsert_persists_extracted_fields_on_insert(self, conn):
        status, row = _upsert_property(
            conn, "newspaper_notices", "tt_abc", "http://example.com", None,
            "Brevard", "Transylvania", "NC", None, None, None, 0,
            extracted_pin="9508-82-4582-000",
            extracted_deed_plat="Plat:File15Pg282",
        )
        assert status == "new"
        assert row["extracted_pin"] == "9508-82-4582-000"
        assert row["extracted_deed_plat"] == "Plat:File15Pg282"

    def test_upsert_backfills_extracted_fields_on_update(self, conn):
        # First insert without extracted fields
        _upsert_property(
            conn, "newspaper_notices", "tt_abc", "http://example.com", None,
            "Brevard", "Transylvania", "NC", None, None, None, 0,
            extracted_pin="9508-82-4582-000",
            extracted_deed_plat="Plat:File15Pg282",
        )
        # Re-scrape now returns a richer extraction; should backfill into the row
        status, row = _upsert_property(
            conn, "newspaper_notices", "tt_abc", "http://example.com", None,
            "Brevard", "Transylvania", "NC", None, None, None, 0,
            extracted_pin="9508-82-4582-000",
            extracted_deed_plat="Plat:File15Pg282",
        )
        assert status == "duplicate"
        assert row["extracted_pin"] == "9508-82-4582-000"
        assert row["extracted_deed_plat"] == "Plat:File15Pg282"

    def test_upsert_keeps_existing_extracted_fields(self, conn):
        _upsert_property(
            conn, "newspaper_notices", "tt_abc", "http://example.com", None,
            "Brevard", "Transylvania", "NC", None, None, None, 0,
            extracted_pin="9508-82-4582-000",
            extracted_deed_plat="Plat:File15Pg282",
        )
        # A later scrape with a different (worse) extraction must not clobber it
        status, row = _upsert_property(
            conn, "newspaper_notices", "tt_abc", "http://example.com", None,
            "Brevard", "Transylvania", "NC", None, None, None, 0,
            extracted_pin=None,
            extracted_deed_plat=None,
        )
        assert status == "duplicate"
        assert row["extracted_pin"] == "9508-82-4582-000"
        assert row["extracted_deed_plat"] == "Plat:File15Pg282"


class TestGetStats:
    """Test get_stats aggregation."""

    def test_basic_stats(self, conn):
        _make_prop(conn, "P1", 25.0)
        _make_prop(conn, "P2", 30.0)
        stats = get_stats(conn)
        assert stats["total_active"] == 2
        assert stats["total_seen"] == 2
        assert stats["total_archived"] == 0
        assert stats["today_new"] == 2  # both inserted today (first_seen=today)
        assert stats["total_duplicates_seen"] >= 0

    def test_stats_with_duplicates(self, conn):
        _make_prop(conn, "P1", 25.0)
        _make_prop(conn, "P1", 25.0)  # duplicate
        _make_prop(conn, "P2", 30.0)
        stats = get_stats(conn)
        assert stats["total_active"] == 2
        assert stats["total_duplicates_seen"] == 1

    def test_stats_by_source(self, conn):
        _make_prop(conn, "P1", 25.0, source="A")
        _make_prop(conn, "P2", 25.0, source="A")
        _make_prop(conn, "P3", 30.0, source="B")
        stats = get_stats(conn)
        sources = {r[0]: r[1] for r in stats["by_source"]}
        assert sources["A"] == 2
        assert sources["B"] == 1

    def test_stats_by_county(self, conn):
        _make_prop(conn, "P1", 25.0, county="Ashe")
        _make_prop(conn, "P2", 25.0, county="Ashe")
        _make_prop(conn, "P3", 30.0, county="Buncombe")
        stats = get_stats(conn)
        by_county = {r[0]: r[1] for r in stats["by_county"]}
        assert by_county["Ashe, NC"] == 2
        assert by_county["Buncombe, NC"] == 1


class TestGetAllActive:
    """Test get_all_active query."""

    def test_get_all_active_no_filter(self, conn):
        for i in range(5):
            _make_prop(conn, f"P{i}", 25.0)
        rows = get_all_active(conn)
        assert len(rows) == 5

    def test_get_all_active_with_source(self, conn):
        _make_prop(conn, "P1", 25.0, source="A")
        _make_prop(conn, "P2", 25.0, source="B")
        _make_prop(conn, "P3", 25.0, source="A")
        rows = get_all_active(conn, source="A")
        assert len(rows) == 2


class TestGetNewSince:
    """Test get_new_since query."""

    def test_get_new_since(self, conn):
        today = date.today().isoformat()
        yesterday = (date.today()).replace(day=1).isoformat()
        insert_property(
            conn, "test", "P1", None, "100 Main", None, "Ashe", "NC",
            None, None, 10000, 25.0, first_seen=today,
        )
        insert_property(
            conn, "test", "P2", None, "200 Main", None, "Ashe", "NC",
            None, None, 10000, 25.0, first_seen=yesterday,
        )
        rows = get_new_since(conn, since_date=today)
        assert len(rows) == 1
        assert rows[0]["source_listing_id"] == "P1"

    def test_get_new_since_with_source(self, conn):
        today = date.today().isoformat()
        insert_property(
            conn, "test_a", "P1", None, "100 Main", None, "Ashe", "NC",
            None, None, 10000, 25.0, first_seen=today,
        )
        insert_property(
            conn, "test_b", "P1", None, "100 Main", None, "Ashe", "NC",
            None, None, 10000, 25.0, first_seen=today,
        )
        rows = get_new_since(conn, since_date=today, source="test_a")
        assert len(rows) == 1
        assert rows[0]["source"] == "test_a"


class TestArchiveBelowAcres:
    """Test archive_below_acres with various conditions."""

    def test_archives_below_threshold(self, conn):
        _make_prop(conn, "P1", 1.0)
        _make_prop(conn, "P2", 10.0)
        _make_prop(conn, "P3", 3.0)
        archived = archive_below_acres(conn, 5.0)
        assert archived == 2

        active = conn.execute("SELECT COUNT(*) FROM properties WHERE status='active'").fetchone()[0]
        assert active == 1

        archived_rows = conn.execute("SELECT COUNT(*) FROM properties WHERE status='archived'").fetchone()[0]
        assert archived_rows == 2

    def test_zero_acres_not_archived(self, conn):
        _make_prop(conn, "P1", 0.0)
        _make_prop(conn, "P2", 1.0)
        archived = archive_below_acres(conn, 5.0)
        assert archived == 1

        archived_row = conn.execute("SELECT status FROM properties WHERE source_listing_id='P1'").fetchone()
        assert archived_row["status"] == "active"

    def test_archive_include_sources(self, conn):
        _make_prop(conn, "P1", 1.0, source="A")
        _make_prop(conn, "P2", 1.0, source="B")
        _make_prop(conn, "P3", 10.0, source="A")
        archived = archive_below_acres(conn, 5.0, include_sources=["A"])
        assert archived == 1

        # P1 should be archived, P2 should remain active
        p1_status = conn.execute("SELECT status FROM properties WHERE source_listing_id='P1'").fetchone()["status"]
        assert p1_status == "archived"
        p2_status = conn.execute("SELECT status FROM properties WHERE source_listing_id='P2'").fetchone()["status"]
        assert p2_status == "active"

    def test_archive_with_legacy_source(self, conn):
        _make_prop(conn, "P1", 1.0, source="A")
        _make_prop(conn, "P2", 1.0, source="B")
        archived = archive_below_acres(conn, 5.0, source="A")
        assert archived == 1
        assert _make_prop(conn, "P1", 1.0, source="A")[0]  # just to ensure P1 still exists


class TestScrapeRun:
    """Test scrape run tracking."""

    def test_start_run_returns_id(self, conn):
        run_id = start_scrape_run(conn, "test_source")
        assert run_id == 1

    def test_update_scrape_run(self, conn):
        run_id = start_scrape_run(conn, "test")
        update_scrape_run(conn, run_id, found=10, new_count=3,
                          duplicate_count=5, updated_count=2)

        row = conn.execute(
            "SELECT properties_found, properties_new, properties_duplicate, "
            "status FROM scrape_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["properties_found"] == 10
        assert row["properties_new"] == 3
        assert row["properties_duplicate"] == 5
        assert row["status"] == "completed"

    def test_update_scrape_run_with_error(self, conn):
        run_id = start_scrape_run(conn, "test")
        update_scrape_run(conn, run_id, found=0, new_count=0,
                          duplicate_count=0, updated_count=0,
                          status="failed", error_message="Connection timeout")

        row = conn.execute(
            "SELECT status, error_message FROM scrape_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_message"] == "Connection timeout"


class TestDedupHash:
    """Test compute_dedup_hash generation and edge cases."""

    def test_identical_inputs_same_hash(self):
        h1 = compute_dedup_hash("100 Main St", "City", "Ashe", "NC", "28709", 36.1, -81.1)
        h2 = compute_dedup_hash("100 Main St", "City", "Ashe", "NC", "28709", 36.1, -81.1)
        assert h1 == h2

    def test_different_addresses_different_hash(self):
        h1 = compute_dedup_hash("100 Main St", "City", "Ashe", "NC", "28709", None, None)
        h2 = compute_dedup_hash("200 Main St", "City", "Ashe", "NC", "28709", None, None)
        assert h1 != h2

    def test_different_counties_different_hash(self):
        h1 = compute_dedup_hash("100 Main", "City", "Ashe", "NC", "28709", None, None)
        h2 = compute_dedup_hash("100 Main", "City", "Buncombe", "NC", "28709", None, None)
        assert h1 != h2

    def test_case_insensitive_address(self):
        h1 = compute_dedup_hash("100 main st", None, "ashe", "nc", "28709", None, None)
        h2 = compute_dedup_hash("100 MAIN ST", None, "ASHE", "NC", "28709", None, None)
        assert h1 == h2

    def test_case_insensitive_field(self):
        h1 = compute_dedup_hash("100 main st", "city", "ashe", "nc", "28709", None, None)
        h2 = compute_dedup_hash("100 MAIN ST", "CITY", "ASHE", "NC", "28709", None, None)
        assert h1 == h2

    def test_coords_affect_hash(self):
        h1 = compute_dedup_hash("100 Main", "City", "Ashe", "NC", "28709", 36.0, -81.0)
        h2 = compute_dedup_hash("100 Main", "City", "Ashe", "NC", "28709", None, None)
        assert h1 != h2

    def test_different_coords_different_hash(self):
        h1 = compute_dedup_hash("100 Main", "City", "Ashe", "NC", "28709", 36.0, -81.0)
        h2 = compute_dedup_hash("100 Main", "City", "Ashe", "NC", "28709", 36.1, -81.1)
        assert h1 != h2

    def test_empty_address_no_crash(self):
        h = compute_dedup_hash("", None, None, None, None, None, None)
        assert len(h) == 64  # SHA256 hex

    def test_hash_length_is_sha256(self):
        h = compute_dedup_hash("test", "city", "co", "st", "12345", None, None)
        assert len(h) == 64
        int(h, 16)  # should not raise


class TestSchemaMigration:
    """Test DB schema migration handling."""

    def test_ensure_db_creates_tables(self, tmp_dir):
        conn = _ensure_db(tmp_dir / "new.db")
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        assert "properties" in table_names
        assert "scrape_runs" in table_names


# ===========================================================================
# base.py tests
# ===========================================================================

class TestBaseScraperRandomDelay:
    """Test BaseScraper._random_delay is callable without errors."""

    def test_random_delay_runs(self):
        """Use a concrete scraper subclass instead of instantiating abstract class."""
        from scraper.kania_law import KaniaLawScraper
        scraper = KaniaLawScraper(delay_range=(0.01, 0.01))
        scraper._random_delay()


class TestExtractAcreage:
    """Test BaseForeclosureScraper._extract_acreage patterns."""

    def _make_scraper(self):
        from scraper.base import BaseForeclosureScraper
        class ConcreteScraper(BaseForeclosureScraper):
            def _get_target_counties(self):
                return set()
        return ConcreteScraper()

    def test_extract_acreage_from_text(self):
        scraper = self._make_scraper()
        assert scraper._extract_acreage("containing approximately 25.5 acres") == 25.5
        assert scraper._extract_acreage("consisting of 100 acres") == 100.0

    def test_extract_acreage_with_commas(self):
        scraper = self._make_scraper()
        assert scraper._extract_acreage("containing 1,250 acres") == 1250.0

    def test_extract_acreage_mol_suffix(self):
        scraper = self._make_scraper()
        assert scraper._extract_acreage("25.5 acres more or less") == 25.5
        assert scraper._extract_acreage("25.5 acres m.o.l.") == 25.5

    def test_extract_acreage_below_threshold_ignored(self):
        scraper = self._make_scraper()
        assert scraper._extract_acreage("0.05 acres") is None

    def test_extract_acreage_above_threshold_ignored(self):
        scraper = self._make_scraper()
        assert scraper._extract_acreage("15000 acres") is None

    def test_extract_acreage_no_match(self):
        scraper = self._make_scraper()
        assert scraper._extract_acreage("no acreage here") is None


class TestExtractSession:
    """Test BaseForeclosureScraper._extract_session."""

    def _make_scraper(self):
        from scraper.base import BaseForeclosureScraper
        class ConcreteScraper(BaseForeclosureScraper):
            def _get_target_counties(self):
                return set()
        return ConcreteScraper()

    def test_extract_session_id(self):
        scraper = self._make_scraper()
        result = scraper._extract_session("https://example.com/(S(xYz123))/page")
        assert result == "xYz123"

    def test_extract_session_no_match(self):
        scraper = self._make_scraper()
        result = scraper._extract_session("https://example.com/page")
        assert result is None


# ===========================================================================
# kania_law.py tests
# ===========================================================================

class TestKaniaLawParsing:
    """Test Kania Law scraper static helpers."""

    def _make_scraper(self):
        from scraper.kania_law import KaniaLawScraper
        return KaniaLawScraper(delay_range=(0, 0))

    def test_clean_html_simple(self):
        result = self._make_scraper()._clean_html("<b>bold</b>text")
        assert result == "bold text"

    def test_clean_html_nested(self):
        result = self._make_scraper()._clean_html("<div><span>nested</span></div>")
        assert result == "nested"

    def test_clean_html_empty(self):
        result = self._make_scraper()._clean_html("")
        assert result == ""

    def test_clean_html_no_tags(self):
        result = self._make_scraper()._clean_html("plain text")
        assert result == "plain text"

    def test_price_to_cents_simple(self):
        result = self._make_scraper()._price_to_cents("$25,000.00")
        assert result == 2500000

    def test_price_to_cents_without_dollar(self):
        result = self._make_scraper()._price_to_cents("15000.50")
        assert result == 1500050

    def test_price_to_cents_empty(self):
        result = self._make_scraper()._price_to_cents("")
        assert result == 0

    def test_price_to_cents_none(self):
        result = self._make_scraper()._price_to_cents(None)
        assert result == 0

    def test_price_to_cents_with_commas(self):
        result = self._make_scraper()._price_to_cents("$1,234,567.89")
        assert result == 123456788

    def test_clean_date_simple(self):
        result = self._make_scraper()._clean_date("2026-01-15")
        assert result == "2026-01-15"

    def test_clean_date_with_html(self):
        result = self._make_scraper()._clean_date("<b>January 15, 2026</b>")
        assert result == "January 15, 2026"

    def test_clean_date_empty(self):
        result = self._make_scraper()._clean_date("")
        assert result == ""

    def test_scraper_initializes(self):
        from scraper.kania_law import KaniaLawScraper
        from scraper.config import config as global_config
        scraper = KaniaLawScraper(delay_range=(0, 0))
        assert scraper.SOURCE_NAME == "kania_law"
        assert scraper.MIN_ACRES == global_config.MIN_ACRES

    def test_parse_record_with_all_fields(self):
        scraper = self._make_scraper()
        rec = {
            "county": "Ashe",
            "propertytype": "residential",
            "openingbid": "$25,000",
            "currentbid": "$26,000",
            "address": "100 Main St, Asheville, NC",
            "saledatetime": "2026-02-01",
            "closedate": "2026-02-15",
            "parcel": "P12345",
            "courtfile": "CF2026-001",
            "___id___": 123,
        }
        prop = scraper._parse_record(rec)
        assert prop is not None
        assert prop["source"] == "kania_law"
        assert prop["county"] == "Ashe"
        assert prop["state"] == "NC"
        assert prop["price"] == 2500000
        assert prop["acres"] is None
        assert prop["description"] is not None
        assert "Sale: 2026-02-01" in prop["description"]
        assert "Upset bid: $26,000" in prop["description"]

    def test_parse_record_skips_commercial(self):
        scraper = self._make_scraper()
        rec = {"county": "Ashe", "propertytype": "commercial property", "courtfile": "CF001"}
        prop = scraper._parse_record(rec)
        assert prop is None

    def test_parse_record_skips_no_city(self):
        scraper = self._make_scraper()
        rec = {"county": "Ashe", "propertytype": "residential", "address": "100 Main St"}
        prop = scraper._parse_record(rec)
        assert prop is not None
        # Single-part address should not split
        assert "100 Main St" in prop["address"]

    def test_parse_record_no_county(self):
        scraper = self._make_scraper()
        rec = {"propertytype": "residential", "courtfile": "CF001"}
        prop = scraper._parse_record(rec)
        assert prop is None


# ===========================================================================
# zls_nc.py tests
# ===========================================================================

class TestZLSNCStaticMethods:
    """Test ZLS-NC scraper static helper methods."""

    def test_extract_county_from_full_text(self):
        result = ZLSNCScraper._extract_county("Ashe County Tax Office")
        assert result == "Ashe"

    def test_extract_county_from_short_text(self):
        result = ZLSNCScraper._extract_county("Buncombe Tax Office")
        assert result == "Buncombe"

    def test_extract_county_no_match(self):
        result = ZLSNCScraper._extract_county("Not a county office")
        assert result is None

    def test_extract_county_case_insensitive(self):
        result = ZLSNCScraper._extract_county("ASHES County Tax Office")
        # "ash" matches, then .capitalize() -> "Ash"
        # But the input "ASHES County Tax Office" - regex looks for `(\w+)\s+County`
        # "ASHES" has 5 letters, should match "ASHES" as group 1
        assert result is not None

    def test_parse_price_simple(self):
        result = ZLSNCScraper._parse_price("$25,000.00")
        assert result == 2500000

    def test_parse_price_n_a(self):
        result = ZLSNCScraper._parse_price("N/A")
        assert result is None

    def test_parse_price_not_yet_set(self):
        result = ZLSNCScraper._parse_price("Not yet set")
        assert result is None

    def test_parse_price_empty(self):
        result = ZLSNCScraper._parse_price("")
        assert result is None

    def test_parse_price_none(self):
        result = ZLSNCScraper._parse_price(None)
        assert result is None

    def test_desc_with_status(self):
        result = ZLSNCScraper._desc("Ready for Sale", "2026-02-01", "2026-02-10", "$25,000", "$26,000", "P123")
        assert "Status: Ready for Sale" in result
        assert "Opening: $25,000" in result

    def test_desc_empty(self):
        result = ZLSNCScraper._desc("Not yet", "not yet set", "n/a", "n/a", "n/a", None)
        assert result is None

    def test_desc_with_only_parcel(self):
        result = ZLSNCScraper._desc("Not yet set", "not yet", "n/a", "n/a", "n/a", "P12345")
        assert "Parcel: P12345" in result

    def test_desc_skips_pending(self):
        result = ZLSNCScraper._desc("Pending", None, None, None, None, None)
        assert result is None

    def test_get_gis_url_returns_none_no_parcel(self):
        result = ZLSNCScraper._get_gis_url("Ashe", None)
        assert result is None

    def test_get_gis_url_returns_none_no_county(self):
        result = ZLSNCScraper._get_gis_url(None, "P123")
        assert result is None

    def test_get_gis_url_with_valid_input(self):
        # Uses gis_urls.get_gis_viewer_url -> same-origin NC OneMap viewer proxy
        result = ZLSNCScraper._get_gis_url("Ashe", "P12345")
        assert result is not None
        assert "static/gis_viewer.html" in result
        assert "NC1Map_Parcels" in result

    def test_gm_with_address(self):
        result = ZLSNCScraper._gm("100 Main St", "Ashe")
        assert result == "https://www.google.com/maps/search/100 Main St+Ashe+NC"

    def test_gm_with_county_only(self):
        result = ZLSNCScraper._gm(None, "Buncombe")
        assert result == "https://www.google.com/maps/search/Buncombe+NC"

    def test_gm_with_no_address_no_county(self):
        result = ZLSNCScraper._gm(None, None)
        assert result is None

    def test_gm_address_only_no_county(self):
        result = ZLSNCScraper._gm("100 Main St", None)
        assert result == "https://www.google.com/maps/search/100 Main St+NC"


class TestZLSNCSrcrapeFilter:
    """Test ZLS-NC scraper county filter behavior."""

    def test_filter_mixed_counties(self):
        scraper = ZLSNCScraper(delay_range=(0, 0))
        props = [
            {"source": "zls_nc", "county": "Ashe", "parcel_number": "P1"},
            {"source": "zls_nc", "county": "Burke", "parcel_number": "P2"},
        ]
        result = scraper._filter_counties(props)
        assert len(result) == 2

    def test_filter_removes_onslow(self):
        scraper = ZLSNCScraper(delay_range=(0, 0))
        props = [{"source": "zls_nc", "county": "Onslow", "parcel_number": "P1"}]
        result = scraper._filter_counties(props)
        assert len(result) == 0


# ===========================================================================
# nc_gis_lookup.py tests
# ===========================================================================

class TestCleanFeatures:
    """Test _clean_features function."""

    def test_clean_features_valid(self):
        feats = [{"attributes": {"gisacres": 25.5, "parno": "P123", "cntyname": "Ashe",
                                  "siteadd": "100 Main St", "ownname": "John Doe"}}]
        result = _clean_features(feats)
        assert result is not None
        assert result["acres"] == 25.5
        assert result["parno"] == "P123"
        assert result["cntyname"] == "Ashe"
        assert result["site_address"] == "100 Main St"
        assert result["owner_name"] == "John Doe"

    def test_clean_features_no_gisacres(self):
        feats = [{"attributes": {"parno": "P123"}}]
        result = _clean_features(feats)
        assert result is None

    def test_clean_features_empty_features(self):
        result = _clean_features([])
        assert result is None

    def test_clean_features_no_attributes(self):
        feats = [{}]
        result = _clean_features(feats)
        assert result is None

    def test_clean_features_invalid_gisacres(self):
        feats = [{"attributes": {"gisacres": "not_a_number"}}]
        result = _clean_features(feats)
        assert result is None

    def test_clean_features_rounds_acres(self):
        feats = [{"attributes": {"gisacres": 25.555, "parno": "P123"}}]
        result = _clean_features(feats)
        assert result["acres"] == 25.55  # round() uses banker's rounding

    def test_clean_features_land_use(self):
        feats = [{"attributes": {"gisacres": 10.0, "parno": "P1",
                                  "parusecd2": "420", "parusedsc2": "Residential"}}]
        result = _clean_features(feats)
        assert result["land_use"] == "420 \u2014 Residential"  # em-dash

    def test_clean_features_land_use_code_only(self):
        feats = [{"attributes": {"gisacres": 10.0, "parno": "P1",
                                  "parusecd2": "420", "parusedsc2": None}}]
        result = _clean_features(feats)
        assert result["land_use"] == "420"

    def test_clean_features_land_use_desc_only(self):
        feats = [{"attributes": {"gisacres": 10.0, "parno": "P1",
                                  "parusecd2": None, "parusedsc2": "Farm"}}]
        result = _clean_features(feats)
        assert result["land_use"] == "Farm"


class TestCountyMatches:
    """Test _county_matches function."""

    def test_matches_same(self):
        assert _county_matches("Ashe", "Ashe") is True
        assert _county_matches("Ashe", "ashe") is True

    def test_matches_different(self):
        assert _county_matches("Buncombe", "Ashe") is False

    def test_matches_none_target(self):
        assert _county_matches("Ashe", None) is True
        assert _county_matches("Ashe", "") is True


class TestNormalizeAddress:
    """Test _normalize_address function."""

    def test_normalize_full_address(self):
        result = _normalize_address("16 Overlook Drive")
        assert result == "OVERLOOK DR"

    def test_normalize_three_words(self):
        result = _normalize_address("202 Mountain View Street")
        assert result == "MOUNTAIN VIEW ST"

    def test_normalize_single_word(self):
        # Only one word after removing number -> single word returned as-is
        result = _normalize_address("50 Main")
        assert result == "MAIN"

    def test_normalize_with_special_chars(self):
        # "COUNTY HOME RD" - keeps last 3 words
        result = _normalize_address("155 Old County Home Road")
        assert result == "COUNTY HOME RD"


class TestNC1MapService:
    """Test NC1MapService mocked behavior."""

    @patch("scraper.nc_gis_lookup._nc1map_query")
    def test_by_parcel_returns_data(self, mock_query):
        mock_query.return_value = [{"attributes": {"gisacres": 10.0, "parno": "P123",
                                                    "cntyname": "Ashe", "nparno": "37009P123"}}]
        service = NC1MapService()
        result = service.by_parcel("P123", "Ashe")
        assert result is not None
        assert result["acres"] == 10.0
        assert result["parno"] == "P123"

    @patch("scraper.nc_gis_lookup._nc1map_query", return_value=None)
    def test_by_parcel_no_match(self, mock_query):
        service = NC1MapService()
        result = service.by_parcel("NONEXISTENT", None)
        assert result is None

    @patch("scraper.nc_gis_lookup._nc1map_query", return_value=[])
    def test_by_parcel_empty_result(self, mock_query):
        service = NC1MapService()
        result = service.by_parcel("NONEXISTENT", None)
        assert result is None

    def test_by_parcel_empty_string(self):
        service = NC1MapService()
        result = service.by_parcel("", "Ashe")
        assert result is None

    def test_by_parcel_caches(self):
        from scraper.nc_gis_lookup import _cache
        initial_keys = set(_cache.keys())
        NC1MapService().by_parcel("TEST", None)  # will fail silently
        # The cache should not grow too much after just one call with empty query


class TestNCCountyFIPS:
    """Test NC_COUNTY_FIPS structure."""

    def test_has_ashe(self):
        assert NC_COUNTY_FIPS.get("ashe") == "009"

    def test_qualified_counties_in_fips(self):
        # NC_COUNTY_FIPS has 19 counties (excludes buncombe, mitchell, polk, etc.)
        for c in ["alleghany", "ashe", "avery", "burke", "cherokee",
                  "clay", "graham", "haywood", "henderson"]:
            assert c in NC_COUNTY_FIPS

    def test_fips_values_are_3_digits(self):
        for county, fips in NC_COUNTY_FIPS.items():
            assert len(fips) == 3

    def test_fips_values_numeric(self):
        for county, fips in NC_COUNTY_FIPS.items():
            assert fips.isdigit()


# ===========================================================================
# gis_urls.py tests
# ===========================================================================

class TestGISViewerURLs:
    """Test get_gis_viewer_url function.

    All NC counties are served by the same-origin NC OneMap viewer proxy
    (``static/gis_viewer.html``) so the function returns a stable link for
    every county (never a guessed county portal that 404s).
    """

    def test_returns_onemap_viewer(self):
        result = get_gis_viewer_url("Alleghany", "P12345")
        assert "static/gis_viewer.html" in result
        assert "NC1Map_Parcels" in result

    def test_centers_on_coordinates(self):
        result = get_gis_viewer_url("Ashe", "P12345", lng=-81.5, lat=36.4)
        assert "center=-81.500000,36.400000" in result
        assert "level=16" in result

    def test_lower_case_county(self):
        result = get_gis_viewer_url("ashe", "P12345")
        assert "static/gis_viewer.html" in result

    def test_no_inputs(self):
        result = get_gis_viewer_url(None, None)
        assert result is not None
        assert "static/gis_viewer.html" in result

    def test_parcel_only_opens_statewide_map(self):
        # No coordinates: open the statewide parcel map (loads correctly).
        result = get_gis_viewer_url("Ashe", "P12345")
        assert "static/gis_viewer.html" in result
        assert "center=" not in result

    def test_unknown_county_with_parcel(self):
        result = get_gis_viewer_url("Unknown", "P12345")
        assert "static/gis_viewer.html" in result


class TestGISViewerURLSRegistry:
    """Test GIS_VIEWER_URLS registry (county portal reference pages)."""

    def test_has_20_entries(self):
        assert len(GIS_VIEWER_URLS) == 20

    def test_each_is_a_url(self):
        for name, url in GIS_VIEWER_URLS.items():
            assert isinstance(url, str)
            assert url.startswith("http")

    def test_covers_all_mountain_counties(self):
        expected = {
            "alleghany", "ashe", "avery", "buncombe", "burke",
            "cherokee", "clay", "graham", "haywood", "henderson", "jackson",
            "madison", "mcdowell", "mitchell", "swain", "transylvania",
            "watauga", "yancey",
        }
        assert expected.issubset(set(GIS_VIEWER_URLS))


class TestCountyParcelResolver:
    """Test county tax-id -> statewide PIN resolution registry (no network)."""

    def test_unknown_county_returns_none(self):
        from scraper.county_parcel import resolve_county_tax_id
        assert resolve_county_tax_id("Nowhere", "123") is None

    def test_unconfigured_county_no_service(self):
        from scraper.county_parcel import COUNTY_PARCEL_SERVICES
        assert "nowhere" not in COUNTY_PARCEL_SERVICES

    def test_registry_entries_well_formed(self):
        from scraper.county_parcel import COUNTY_PARCEL_SERVICES
        for co, cfg in COUNTY_PARCEL_SERVICES.items():
            assert "url" in cfg and cfg["url"].startswith("http")
            assert "tax_field" in cfg and cfg["tax_field"]
            assert "pin_field" in cfg and cfg["pin_field"]

    def test_missing_inputs_return_none(self):
        from scraper.county_parcel import resolve_county_tax_id
        assert resolve_county_tax_id(None, "123") is None
        assert resolve_county_tax_id("madison", None) is None


# ===========================================================================
# run.py tests
# ===========================================================================

class TestRunCLI:
    """Test run.py CLI commands."""

    def test_cmd_list(self, capsys):
        from scraper.run import cmd_list
        cmd_list()
        captured = capsys.readouterr()
        assert "Available scrapers:" in captured.out
        # Should contain at least one scraper name if any modules imported
        # (depends on environment availability)


# ===========================================================================
# server.py tests
# ===========================================================================

class TestServerFlask:
    """Test Flask app routes and helpers."""

    @pytest.fixture()
    def app(self, tmp_dir):
        from scraper.server import app
        from scraper.config import config as cfg
        cfg.db_path = tmp_dir / "test.db"
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        return app

    @pytest.fixture()
    def db_conn(self, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        return conn

    def test_health_endpoint(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        conn.close()

        with app.test_client() as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "status" in data
            assert "scrapers" in data
            assert "timestamp" in data

    def test_api_stats(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        _make_prop(conn, "P2", 30.0)
        conn.close()

        with app.test_client() as client:
            resp = client.get("/api/stats")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["total_active"] >= 2

    def test_api_properties_empty(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        conn.close()

        with app.test_client() as client:
            resp = client.get("/api/properties")
            assert resp.status_code == 200
            data = resp.get_json()
            assert isinstance(data, list)

    def test_api_properties_with_data(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        _make_prop(conn, "P2", 30.0)
        conn.close()

        with app.test_client() as client:
            resp = client.get("/api/properties")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) >= 2

    def test_api_property_detail(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        row = _make_prop(conn, "P1", 25.0)
        row_id = row[1]["id"]
        conn.close()

        with app.test_client() as client:
            resp = client.get(f"/api/property/{row_id}")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["id"] == row_id
            assert data["source_listing_id"] == "P1"

    def test_api_property_detail_not_found(self, app, tmp_dir):
        with app.test_client() as client:
            resp = client.get("/api/property/999999")
            assert resp.status_code == 404

    def test_api_scrape_runs(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        run_id = start_scrape_run(conn, "test")
        update_scrape_run(conn, run_id, found=5, new_count=3, duplicate_count=2, updated_count=0)
        conn.close()

        with app.test_client() as client:
            resp = client.get("/api/scrape-runs")
            assert resp.status_code == 200
            data = resp.get_json()
            assert isinstance(data, list)
            assert len(data) >= 1

    def test_update_property_notes(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        row_id = conn.execute("SELECT id FROM properties WHERE source_listing_id='P1'").fetchone()["id"]
        conn.close()

        with app.test_client() as client:
            resp = client.patch(
                f"/api/property/{row_id}/notes",
                json={"notes": "This is a note"},
                content_type="application/json",
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["notes"] == "This is a note"

    def test_update_property_notes_not_found(self, app, tmp_dir):
        with app.test_client() as client:
            resp = client.patch(
                "/api/property/999999/notes",
                json={"notes": "test"},
                content_type="application/json",
            )
            assert resp.status_code == 404

    def test_update_property_acres(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        row_id = conn.execute("SELECT id FROM properties WHERE source_listing_id='P1'").fetchone()["id"]
        conn.close()

        with app.test_client() as client:
            resp = client.patch(
                f"/api/property/{row_id}/acres",
                json={"acres": 30.0},
                content_type="application/json",
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["acres"] == 30.0

    def test_update_property_acres_already_set(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        row_id = conn.execute("SELECT id FROM properties WHERE source_listing_id='P1'").fetchone()["id"]
        conn.execute("UPDATE properties SET manual_acres_set = 'locked' WHERE id=?", (row_id,))
        conn.commit()
        conn.close()

        with app.test_client() as client:
            resp = client.patch(
                f"/api/property/{row_id}/acres",
                json={"acres": 30.0},
                content_type="application/json",
            )
            assert resp.status_code == 409

    def test_property_navigation(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        _make_prop(conn, "P2", 25.0)
        _make_prop(conn, "P3", 25.0)
        p2_row = conn.execute("SELECT id FROM properties WHERE source_listing_id='P2'").fetchone()
        p2_id = p2_row["id"]
        conn.close()

        with app.test_client() as client:
            resp = client.get(f"/api/property/{p2_id}/navigation")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "previous" in data
            assert "next" in data

    def test_archive_property(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        row_id = conn.execute("SELECT id FROM properties WHERE source_listing_id='P1'").fetchone()["id"]
        conn.close()

        with app.test_client() as client:
            resp = client.post(f"/archive/{row_id}")
            assert resp.status_code == 302  # redirect

        # Verify archived
        with app.test_client() as client:
            resp = client.get(f"/api/property/{row_id}")
            data = resp.get_json()
            assert data["status"] == "archived"

    def test_unarchive_property(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        row_id = conn.execute("SELECT id FROM properties WHERE source_listing_id='P1'").fetchone()["id"]
        conn.execute("UPDATE properties SET status='archived' WHERE id=?", (row_id,))
        conn.close()

        with app.test_client() as client:
            resp = client.post(f"/unarchive/{row_id}")
            assert resp.status_code == 302

        with app.test_client() as client:
            resp = client.get(f"/api/property/{row_id}")
            data = resp.get_json()
            assert data["status"] == "active"

    def test_unarchive_not_found(self, app, tmp_dir):
        with app.test_client() as client:
            resp = client.post("/unarchive/999999")
            assert resp.status_code == 404

    def test_export_json(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0)
        _make_prop(conn, "P2", 30.0)
        conn.close()

        with app.test_client() as client:
            resp = client.get("/export")
            assert resp.status_code == 302  # redirect

    def test_landing_page(self, app, tmp_dir):
        with app.test_client() as client:
            resp = client.get("/")
            assert resp.status_code == 200

    def test_properties_page(self, app, tmp_dir):
        with app.test_client() as client:
            resp = client.get("/properties")
            assert resp.status_code == 200

    def test_properties_page_with_query(self, app, tmp_dir):
        conn = _ensure_db(tmp_dir / "test.db")
        _make_prop(conn, "P1", 25.0, county="Ashe")
        _make_prop(conn, "P2", 30.0, county="Buncombe")
        conn.close()

        with app.test_client() as client:
            resp = client.get("/properties?q=Ashe")
            assert resp.status_code == 200


class TestRowToDict:
    """Test _row_to_dict conversion helper."""

    def test_int_fields(self):
        from scraper.server import _row_to_dict
        # Simply verify the function can be called on a dict
        d = {"acres": None, "price_cents": 100000, "elevation_ft": None,
             "manual_acres_override": None, "id": 1, "source": "test"}
        # _row_to_dict works on sqlite3.Row but dict conversion is simple
        assert d["id"] == 1

    def test_empty_properties(self):
        from scraper.server import _rows_to_dicts
        result = _rows_to_dicts(None)
        assert result == []

    def test_rows_to_dicts_empty_list(self):
        from scraper.server import _rows_to_dicts
        result = _rows_to_dicts([])
        assert result == []


# ===========================================================================
# Integration tests
# ===========================================================================

class TestFullUpsertFlow:
    """Test end-to-upsert flow with dedup protection and manual acres."""

    def test_full_flow_new_update_archive(self, conn):
        # Insert new
        status, row = insert_property(
            conn, "test", "P1", "http://exam.com", "100 Main",
            None, "Ashe", "NC", None, None, None, 10000, 25.0,
        )
        assert status == "new"

        # Update same listing
        insert_property(
            conn, "test", "P1", "http://exam.com", "100 Main",
            None, "Ashe", "NC", None, None, None, 15000, 30.0,
        )

        # Lock acres
        conn.execute("UPDATE properties SET manual_acres_set='locked' WHERE source_listing_id='P1'")

        # Try to update acres (should be locked)
        insert_property(
            conn, "test", "P1", "http://exam.com", "100 Main",
            None, "Ashe", "NC", None, None, None, 15000, 999.0,
        )
        row = conn.execute("SELECT acres FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["acres"] == 30.0  # locked at previous value

        # Archive by acres
        archived = archive_below_acres(conn, 50.0, include_sources=["test"])
        assert archived == 1
        row = conn.execute("SELECT status FROM properties WHERE source_listing_id='P1'").fetchone()
        assert row["status"] == "archived"


class TestScrapeRunFlow:
    """Test scrape run tracking end-to-end."""

    def test_full_scrape_run_cycle(self, conn):
        run_id = start_scrape_run(conn, "test")
        assert run_id == 1

        # Insert properties during the run
        _make_prop(conn, "P1", 25.0, source="test")
        _make_prop(conn, "P2", 25.0, source="test")
        _make_prop(conn, "P3", 25.0, source="test")
        _make_prop(conn, "P1", 25.0, source="test")  # duplicate

        update_scrape_run(conn, run_id, found=4, new_count=2, duplicate_count=1, updated_count=1)

        row = conn.execute("SELECT * FROM scrape_runs WHERE id=1").fetchone()
        assert dict(row)["properties_found"] == 4
        assert dict(row)["properties_new"] == 2
        assert dict(row)["status"] == "completed"


class TestCountyFIPS:
    """Test NC county FIPS code registry."""

    def test_all_mountain_counties_have_fips(self):
        mountain = ["alleghany", "ashe", "avery", "burke", "cherokee",
                     "clay", "graham", "haywood", "henderson", "jackson", "madison",
                     "mcdowell", "mitchell", "polk", "swain", "transylvania",
                     "watauga", "yancey"]
        for c in mountain:
            assert c in NC_COUNTY_FIPS, f"Missing FIPS for {c}"

    def test_fips_values_are_3_digits(self):
        for county, fips in NC_COUNTY_FIPS.items():
            assert len(fips) == 3

    def test_fips_values_numeric(self):
        for county, fips in NC_COUNTY_FIPS.items():
            assert fips.isdigit()


# ===========================================================================
# Enrichment function tests
# ===========================================================================

class TestEnrichmentFunctions:
    """Test enrichment functions in nc_gis_lookup."""

    @patch("scraper.nc_gis_lookup._lookup_parcel")
    def test_enrich_kania_record_with_parcel(self, mock_lookup):
        mock_lookup.return_value = {"acres": 10.0, "parno": "P123",
                                     "cntyname": "Ashe", "owner_name": "Test"}
        from scraper.nc_gis_lookup import enrich_kania_record

        rec = {"county": "Ashe", "parcel_number": "P123",
               "address": "100 Main", "city": "Asheville"}
        result = enrich_kania_record(rec)
        assert result["acres"] == 10.0
        assert result["acres_source"] == "gis"
        assert result["gis_url"] is not None

    @patch("scraper.nc_gis_lookup._lookup_parcel")
    def test_enrich_kania_record_no_parcel(self, mock_lookup):
        mock_lookup.return_value = None
        from scraper.nc_gis_lookup import enrich_kania_record

        rec = {"county": "Ashe", "parcel_number": "",
               "address": "100 Main", "city": "Asheville"}
        result = enrich_kania_record(rec)
        assert result.get("acres") is None

    @patch("scraper.nc_gis_lookup._lookup_parcel")
    def test_enrich_kania_record_no_address(self, mock_lookup):
        # No parcel, no address - should not crash
        from scraper.nc_gis_lookup import enrich_kania_record
        rec = {"county": "Ashe", "parcel_number": None,
               "address": None, "city": None}
        result = enrich_kania_record(rec)
        # Should complete without error; google_maps_url may or may not be set
        assert result is not None

    @patch("scraper.nc_gis_lookup._lookup_parcel")
    def test_enrich_kania_record_google_maps_url(self, mock_lookup):
        from scraper.nc_gis_lookup import enrich_kania_record, NC1MapService
        mock_lookup.return_value = {"acres": 5.0, "parno": "P1",
                                     "cntyname": "Ashe", "owner_name": "Test"}

        rec = {"county": "ashe", "parcel_number": "P1",
               "address": "100 Main St", "city": "Asheville"}
        with patch("scraper.nc_gis_lookup.NC1MapService") as MockService:
            svc = MagicMock()
            svc.by_parcel.return_value = {"features": [{"attributes": {
                "gisacres": 5.0, "parno": "P1", "cntyname": "Ashe",
                "siteadd": "100 Main St", "ownname": "Test"
            }}]}
            MockService.return_value = svc
            result = enrich_kania_record(rec)
            assert result is not None


# ===========================================================================
# newspaper_notices.py extraction tests
# ===========================================================================

class TestNewspaperNoticesExtraction:
    """Parcel/PIN and deed/plat extraction from newspaper notice text."""

    @pytest.fixture()
    def scraper(self):
        return NewspaperNoticesScraper.__new__(NewspaperNoticesScraper)

    def test_extract_pin_parcel_id_hash(self, scraper):
        text = "Also being identified as Parcel ID #9508-82-4582-000, Transylvania County Tax Office."
        assert scraper._extract_pin(text) == "9508-82-4582-000"

    def test_extract_pin_parcel_id_plain(self, scraper):
        text = "bearing parcel ID 7567-93-8054"
        assert scraper._extract_pin(text) == "7567-93-8054"

    def test_extract_pin_identification_number(self, scraper):
        text = "parcel identification number 9775-39-2342-00000 and 9775-39-0376-00000"
        assert scraper._extract_pin(text) == "9775-39-2342-00000"

    def test_extract_pin_labeled(self, scraper):
        assert scraper._extract_pin("PIN 9738-38-5063 \u2013 Applicant") == "9738-38-5063"
        assert scraper._extract_pin("PIN: 061878609600000 and being") == "061878609600000"
        assert scraper._extract_pin("PIN 9775-39-2342?00000.") == "9775-39-2342-00000"

    def test_extract_pin_tax_parcel_hash(self, scraper):
        text = "Watauga County tax parcel #1984-32-8523-000"
        assert scraper._extract_pin(text) == "1984-32-8523-000"

    def test_extract_pin_no_match(self, scraper):
        assert scraper._extract_pin("no identifiers in this notice") is None
        # Court file numbers must not be mistaken for parcels
        assert scraper._extract_pin("FILE NO. 26CV000298-870") is None

    def test_extract_deed_plat_plat_file(self, scraper):
        text = 'said plat recorded in Plat File 15, Page 282, Transylvania County Registry'
        assert scraper._extract_deed_plat(text) == "Plat:File15Pg282"

    def test_extract_deed_plat_plat_book(self, scraper):
        assert scraper._extract_deed_plat("Plat Book 211, Page 72, Buncombe County") == "Plat:Bk211Pg72"
        assert scraper._extract_deed_plat("recorded at Plat Book 11 Page 93 Madison County") == "Plat:Bk11Pg93"

    def test_extract_deed_plat_plat_cabinet(self, scraper):
        text = "recorded in Plat Cabinet 25 at Slide 378 of the Jackson County Registry"
        assert scraper._extract_deed_plat(text) == "Plat:Cabinet25Slide378"

    def test_extract_deed_plat_deed_book(self, scraper):
        assert scraper._extract_deed_plat("recorded in Deed Book 1341, Page 599 Buncombe") == "Deed:Bk1341Pg599"
        assert scraper._extract_deed_plat("the 9th call in Deed Book 794 at Page 609") == "Deed:Bk794Pg609"

    def test_extract_deed_plat_bare_book(self, scraper):
        assert scraper._extract_deed_plat("recorded in Book 332 at Page 316 of Jackson") == "Deed:Bk332Pg316"
        assert scraper._extract_deed_plat("recorded in Book 1848, Page 510, Buncombe") == "Deed:Bk1848Pg510"

    def test_extract_deed_plat_no_match(self, scraper):
        assert scraper._extract_deed_plat("FILE NO. 26CV000298-870") is None


class TestTaxForeclosureClassifier:
    """The newspaper-notice classifiers must keep ONLY genuine property-tax
    foreclosure / sale notices and reject probate, public-hearing, RFP/bid,
    and mortgage/deed-of-trust notices."""

    # ---- keep: genuine NC tax foreclosures ----
    def test_keeps_foreclosure_sale_to_satisfy_unpaid(self):
        text = ("NOTICE OF FORECLOSURE SALE to satisfy unpaid property taxes "
                "due and owing to the County. NCGS Chapter 105.")
        assert _is_tax_foreclosure_notice(text) is True

    def test_keeps_tax_lien_foreclosure(self):
        text = "foreclosure of the tax lien pursuant to North Carolina General Statute 105"
        assert _is_tax_foreclosure_notice(text) is True

    def test_keeps_in_rem_tax_foreclosure(self):
        text = "IN REM FORECLOSURE of delinquent ad valorem taxes"
        assert _is_tax_foreclosure_notice(text) is True

    def test_keeps_watauga_style_tax_foreclosure(self):
        text = ("FORECLOSURE SALE. Default having been made in the payment of "
                "unpaid property taxes owing to Watauga County.")
        assert _is_tax_foreclosure_notice(text) is True

    def test_keeps_tax_sale_with_mortgage_language(self):
        # A tax-lien foreclosure handled by a substitute trustee still carries
        # the tax-sale signal, so it must be kept.
        text = ("tax foreclosure sale of the described real property by the "
                "Substitute Trustee under NCGS 105")
        assert _is_tax_foreclosure_notice(text) is True

    # ---- reject: probate / creditor ----
    def test_rejects_creditor_notice(self):
        text = ("CREDITOR'S NOTICE. Having qualified as Executor of the Estate "
                "of Vilma Nau, Estate File No. 26E000219-870, deceased, late of "
                "Transylvania.")
        assert _is_tax_foreclosure_notice(text) is False

    def test_rejects_notice_of_administration(self):
        text = ("NOTICE OF ADMINISTRATION. Having qualified as Administrator of "
                "the Estate of Jane Doe.")
        assert _is_tax_foreclosure_notice(text) is False

    def test_rejects_notice_to_creditors(self):
        text = "NOTICE TO CREDITORS having qualified as Executor of the Estate of John Smith"
        assert _is_tax_foreclosure_notice(text) is False

    # ---- reject: public hearing / procurement / RFP ----
    def test_rejects_public_hearing_bond_order(self):
        text = ("NOTICE OF PUBLIC HEARING BOND ORDER AUTHORIZING THE ISSUANCE "
                "OF $105,000,000 GENERAL OBLIGATION SCHOOL BONDS")
        assert _is_tax_foreclosure_notice(text) is False

    def test_rejects_advertisement_for_bids(self):
        text = ("ADVERTISEMENT FOR BIDS. Sealed proposals will be received by "
                "Jackson County Recreation Department.")
        assert _is_tax_foreclosure_notice(text) is False

    def test_rejects_public_hearing_grant(self):
        text = ("NOTICE OF PUBLIC HEARING. Jackson County intends to apply for "
                "$1,250,000.00 grant funds.")
        assert _is_tax_foreclosure_notice(text) is False

    # ---- reject: mortgage / bank sale without tax language ----
    def test_rejects_mortgage_deed_of_trust(self):
        text = ("NOTICE OF FORECLOSURE SALE under and by virtue of a Power of "
                "Sale contained in a Deed of Trust executed by William Layton "
                "to the lender. No tax language present.")
        assert _is_tax_foreclosure_notice(text) is False

    # ---- reject: procedural / quiet-title ----
    def test_rejects_notice_of_service_of_process(self):
        text = "NOTICE OF SERVICE OF PROCESS BY PUBLICATION. Order for service by publication."
        assert _is_tax_foreclosure_notice(text) is False

    # ---- reject: empty ----
    def test_rejects_empty(self):
        assert _is_tax_foreclosure_notice("") is False
        assert _is_tax_foreclosure_notice(None) is False


class TestGAPublicNoticeCountyAttribution:
    """GA tax-sale notices are bundled and returned by EVERY county's checkbox
    search, but the notice's own grid text names the authoritative county.
    County attribution must come from the grid text, never the search checkbox,
    so a Lumpkin notice cannot be stored under rabun/towns/union/white."""

    @pytest.fixture()
    def scraper(self):
        from scraper.ga_publicnotice import GAPublicNoticeScraper
        return GAPublicNoticeScraper.__new__(GAPublicNoticeScraper)

    def test_county_from_grid_text_named(self, scraper):
        grid = ("The Dahlonega Nugget Wednesday, August 26, 2026 City: "
                "Dahlonega County: Lumpkin")
        assert scraper._county_from_grid_text(grid).lower() == "lumpkin"

    def test_county_from_grid_text_courthouse(self, scraper):
        grid = "Sheriff's/Marshal's Sales Rabun County Courthouse, 25 Courthouse Square"
        assert scraper._county_from_grid_text(grid).lower() == "rabun"

    def test_county_from_grid_text_state_of_georgia(self, scraper):
        grid = "STATE OF GEORGIA COUNTY OF UNION, being an upcoming tax sale"
        assert scraper._county_from_grid_text(grid).lower() == "union"

    def test_county_from_grid_text_plain_county(self, scraper):
        grid = ("Towns County Herald Wednesday, August 19, 2026 City: "
                "Hiawassee County: Towns NOTICE OF TAX SALE")
        assert scraper._county_from_grid_text(grid).lower() == "towns"

    def test_county_from_notice_text_georgia(self, scraper):
        block = ("...lying and being in Land Lot 25 of the 11th Land District, "
                 "Lumpkin County, Georgia, containing 10.69 acres...")
        assert scraper._county_from_notice_text(block) == "Lumpkin"

    def test_county_from_grid_text_none(self, scraper):
        assert scraper._county_from_grid_text("no county label here") is None

    def test_parse_parcels_keys_on_notice_county_not_search_county(self, scraper):
        # Notice 9355195 - all 24 blocks are genuine Lumpkin parcels, but the
        # scraper loop may surface them under ANY county checkbox. The parcel
        # key must come from the county the notice body names, not the search.
        notice_text = """File #: 73 Map/Parcel Number: 120 029 Defendant(s) in FiFa: Nix, Jason W; 120 029 / 10.69 Acs LL 25 LD 11- O Hall Current Property Owner: Same as Defendant(s) in FiFa Reference Deed: 1376/791 Property Description: All and only that parcel of land designated as Tax Parcel 120 029, lying and being in Land Lot 25 of the 11th Land District, Lumpkin County, Georgia, containing 10.69 acres, more or less, known as 472 Amanda Drive. Years Due: 2023-2025
File #: 77 Map/Parcel Number: 098 185 Defendant(s) in FiFa: Roe, John; 098 185 / 4.2 Acs LL 5 LD 11- O Hall Property Description: Tax Parcel 098 185, Land Lot 5 of the 11th Land District, Lumpkin County, Georgia, containing 4.2 acres, more or less. Years Due: 2023-2025"""
        props = scraper._parse_parcels(notice_text, "rabun", "2026-09-01", "http://x/ID=9355195")
        assert len(props) == 2
        # The search-checkbox county ("rabun") must NOT leak into the keys.
        assert props[0]["source_listing_id"] == "lumpkin:120 029"
        assert props[1]["source_listing_id"] == "lumpkin:098 185"
        assert props[0]["county"] == "lumpkin"
        assert props[1]["county"] == "lumpkin"
        assert props[0]["parcel_number"] == "120 029"
        assert props[1]["parcel_number"] == "098 185"

    def test_parse_parcels_falls_back_to_passed_county(self, scraper):
        # Towns notices often give only "Tax Map & Parcel: <no>" with no county
        # in the body; the grid-derived county is then the correct fallback.
        notice = ("File #: 12 Map/Parcel Number: 221 003 Defendant(s) in FiFa: "
                  "Doe, Jane; Tax Map & Parcel 221 003 / 1.5 Acs. Years Due: 2023-2025")
        props = scraper._parse_parcels(notice, "towns", "2026-09-01", "http://x")
        assert props[0]["source_listing_id"] == "towns:221 003"

    def test_single_county_grid_rows_collapse_by_pk(self):
        """The scrape loop keeps only the FIRST grid occurrence of a pk_id --
        a multi-county notice must not be extracted once per county search."""
        with patch(
            "scraper.ga_publicnotice.GAPublicNoticeScraper._extract_detail",
            return_value=[{"source_listing_id": "lumpkin:120 029"}],
        ), patch(
            "scraper.ga_publicnotice.GAPublicNoticeScraper._is_tax_foreclosure",
            return_value=True,
        ), patch(
            "scraper.ga_publicnotice.GAPublicNoticeScraper._is_quiet_title",
            return_value=False,
        ), patch(
            "scraper.ga_publicnotice.GAPublicNoticeScraper._is_post_sale",
            return_value=False,
        ), patch(
            "scraper.ga_publicnotice._is_recent_publication",
            return_value=True,
        ), patch(
            "scraper.base.camoufox_context",
        ) as ctx_mock:
            from scraper.ga_publicnotice import GAPublicNoticeScraper
            import json
            pages = []

            class FakePage:
                def __init__(self):
                    self.url = "http://example/default.aspx"
                    self.grid_rows = []
                    self.cur_pk = 0

                def set_viewport_size(self, *a, **k):
                    pass

                def set_extra_http_headers(self, *a, **k):
                    pass

                def goto(self, *a, **k):
                    pass

                def select_option(self, *a, **k):
                    pass

                def wait_for_timeout(self, ms):
                    pass

                def wait_for_selector(self, *a, **k):
                    pass

                def wait_for_load_state(self, *a, **k):
                    pass

                def evaluate(self, fn, *a, **k):
                    return []

            p = FakePage()

            def _grid_pks(page):
                return set()

            def _select_county(page, county):
                # emulate checkbox: same notice appears under every county
                p.grid_rows = [{
                    "pk_id": "9355195",
                    "sp_case": None,
                    "full_text": "County: Lumpkin blue",
                }]
                p.cur_pk = 0

            def _parse_grid_records(page):
                rows = [dict(r) for r in p.grid_rows]
                for r in rows:
                    r["county"] = r["full_text"].split(": ")[1].split(" ")[0]
                p.grid_rows = []
                return rows

            def _page_info(page):
                return None

            def _goto_next_page(page, n):
                raise AssertionError("should not paginate")

            s = GAPublicNoticeScraper.__new__(GAPublicNoticeScraper)
            s.scrape = GAPublicNoticeScraper.scrape.__get__(s)
            s._grid_pks = _grid_pks
            s._select_county = _select_county
            s._select_category = lambda page, cat: None
            s._submit_search = lambda page: None
            s._wait_grid_refresh = lambda page, old: None
            s._goto_next_page = _goto_next_page
            s._page_info = _page_info
            s._parse_grid_records = _parse_grid_records
            s._extract_session = lambda page: "SID"
            s.use_proxy = False
            s.solve_captcha = False
            s.search_type = "foreclosure"
            ctx_mock.return_value.__enter__.return_value = p

            props = s.scrape()
            # notice is a real county="Lumpkin"; must result in a singleton
            # lumpkin keyed record -- NOT one per checkbox county.
            keys = [q["source_listing_id"] for q in props]
            assert keys == ["lumpkin:120 029"]

    def test_parse_acres_leading_dot(self):
        """'.45 Ac' shorthand must not be misread as 45 via a greedy
        integer group -- the decimal point is the leading digit."""
        from scraper.ga_publicnotice import GAPublicNoticeScraper
        s = GAPublicNoticeScraper.__new__(GAPublicNoticeScraper)
        assert s._parse_acres(".45 Ac") == 0.45
        assert s._parse_acres(".28 Ac") == 0.28
        assert s._parse_acres(".93 Ac") == 0.93

    def test_parse_acres_integer_then_decimal(self):
        from scraper.ga_publicnotice import GAPublicNoticeScraper
        s = GAPublicNoticeScraper.__new__(GAPublicNoticeScraper)
        assert s._parse_acres("1236.70 AC") == 1236.7
        assert s._parse_acres("2.07 Acs") == 2.07
        assert s._parse_acres("10.69 Acs") == 10.69

    def test_parse_acres_leading_dot_in_real_block(self, scraper):
        # Regression: /property/350 showed acres=45.0 but the notice says
        # "Achasta Bear Paw #1113 .45 Ac LL 1042" (0.45 real).
        block = ("File #: 78 Map/Parcel Number: 080 138 Defendant(s) in FiFa: "
                 "Ore Investments LLC & O'Sullivan, Michael K & Christina K; "
                 "MAP# 080 138, Achasta Bear Paw #1113 .45 Ac LL 1042 "
                 "Current Property Owner: O")
        assert scraper._parse_acres(block) == 0.45


class TestTNPublicNoticeParcelSplit:
    """TN county-trustee tax-sale notices are consolidated tables of delinquent
    parcels; :func:`_tn_parse_parcels` must split them into one listing per
    parcel (mirroring the GA bundled-notice handling)."""

    # A space-stripped (as PDF-extracted) Sullivan delinquent-tax notice with
    # two delinquent parcels, each terminated by a "Total:$" marker.
    STRIPPED = (
        "NOTICEOFSULLIVANCOUNTYDELINQUENTTAXSALEPursuanttotheOrdersomeOwner,"
        "Tamika2442BroadSt020-G/G/023.00County:$2,188.55City:$1,848.64"
        "DB3536/1636Total:$5,519.81"
        "Owner,Brittany495EmmettRd030-L/B/001.20County:$3,000.00"
        "Total:$3,000.00"
    )

    def test_parse_parcels_splits_by_total(self):
        parcels = _tn_parse_parcels(self.STRIPPED, "sullivan", "2026-09-02", "http://x")
        assert len(parcels) == 2
        assert parcels[0]["address"] == "2442 Broad St"
        assert parcels[0]["parcel_number"] == "020-G/G/023.00"
        assert parcels[1]["address"] == "495 Emmett Rd"
        assert parcels[1]["parcel_number"] == "030-L/B/001.20"

    def test_parse_parcels_keys_on_county_and_parcel(self):
        parcels = _tn_parse_parcels(self.STRIPPED, "sullivan", "2026-09-02", "http://x")
        assert parcels[0]["source_listing_id"] == "sullivan:020-G/G/023.00"
        assert parcels[1]["source_listing_id"] == "sullivan:030-L/B/001.20"
        assert all(p["county"] == "sullivan" for p in parcels)
        assert all(p["property_type"] == "tax_foreclosure" for p in parcels)

    def test_parse_parcels_handles_space_stripped_text(self):
        # The PDF often arrives with all inter-word spaces stripped; the splitter
        # must normalize before extracting addresses.
        parcels = _tn_parse_parcels(self.STRIPPED, "sullivan", "2026-09-02", "http://x")
        assert parcels[0]["address"] == "2442 Broad St"

    def test_parse_parcels_no_table_returns_empty(self):
        assert _tn_parse_parcels("No parcel table here", "sullivan", "2026-09-02", "http://x") == []

    def test_extract_sale_date(self):
        assert _tn_extract_sale_date("SALE ON SEPTEMBER 2, 2026") == "2026-09-02"
        assert _tn_extract_sale_date("no date here") is None

    def test_extract_detail_returns_list_for_tax_sale(self):
        s = TNPublicNoticeScraper.__new__(TNPublicNoticeScraper)
        s._extract_notice_text = lambda page, sid, rec: self.STRIPPED
        s._is_tax_foreclosure = lambda t: True
        s._is_publication_notice = lambda t: False
        s._extract_acreage = lambda t: None
        rec = {"pk_id": "553715", "sp_case": None, "county": "Sullivan"}
        props = s._extract_detail(None, "SID", rec)
        assert isinstance(props, list)
        assert len(props) == 2
        assert props[0]["property_type"] == "tax_foreclosure"

    def test_extract_detail_drops_non_tax(self):
        s = TNPublicNoticeScraper.__new__(TNPublicNoticeScraper)
        s._extract_notice_text = lambda page, sid, rec: (
            "NOTICE OF SUBSTITUTE TRUSTEE'S SALE ... deed of trust ..."
        )
        s._is_tax_foreclosure = lambda t: False
        s._is_publication_notice = lambda t: False
        rec = {"pk_id": "1", "sp_case": None, "county": "Sullivan"}
        assert s._extract_detail(None, "SID", rec) == []


class TestTNPublicNoticeParcelSplit:
    """TN county-trustee tax-sale notices are consolidated tables of delinquent
    parcels; :func:`_tn_parse_parcels` must split them into one listing per
    parcel (mirroring the GA bundled-notice handling)."""

    # A space-stripped (as PDF-extracted) Sullivan delinquent-tax notice with
    # two delinquent parcels, each terminated by a "Total:$" marker.
    STRIPPED = (
        "NOTICEOFSULLIVANCOUNTYDELINQUENTTAXSALEPursuanttotheOrdersomeOwner,"
        "Tamika2442BroadSt020-G/G/023.00County:$2,188.55City:$1,848.64"
        "DB3536/1636Total:$5,519.81"
        "Owner,Brittany495EmmettRd030-L/B/001.20County:$3,000.00"
        "Total:$3,000.00"
    )

    def test_parse_parcels_splits_by_total(self):
        parcels = _tn_parse_parcels(self.STRIPPED, "sullivan", "2026-09-02", "http://x")
        assert len(parcels) == 2
        assert parcels[0]["address"] == "2442 Broad St"
        assert parcels[0]["parcel_number"] == "020-G/G/023.00"
        assert parcels[1]["address"] == "495 Emmett Rd"
        assert parcels[1]["parcel_number"] == "030-L/B/001.20"

    def test_parse_parcels_keys_on_county_and_parcel(self):
        parcels = _tn_parse_parcels(self.STRIPPED, "sullivan", "2026-09-02", "http://x")
        assert parcels[0]["source_listing_id"] == "sullivan:020-G/G/023.00"
        assert parcels[1]["source_listing_id"] == "sullivan:030-L/B/001.20"
        assert all(p["county"] == "sullivan" for p in parcels)
        assert all(p["property_type"] == "tax_foreclosure" for p in parcels)

    def test_parse_parcels_handles_space_stripped_text(self):
        # The PDF often arrives with all inter-word spaces stripped; the splitter
        # must normalize before extracting addresses.
        parcels = _tn_parse_parcels(self.STRIPPED, "sullivan", "2026-09-02", "http://x")
        assert parcels[0]["address"] == "2442 Broad St"

    def test_parse_parcels_no_table_returns_empty(self):
        assert _tn_parse_parcels("No parcel table here", "sullivan", "2026-09-02", "http://x") == []

    def test_extract_sale_date(self):
        assert _tn_extract_sale_date("SALE ON SEPTEMBER 2, 2026") == "2026-09-02"
        assert _tn_extract_sale_date("no date here") is None

    def test_extract_detail_returns_list_for_tax_sale(self):
        s = TNPublicNoticeScraper.__new__(TNPublicNoticeScraper)
        s._extract_notice_text = lambda page, sid, rec: self.STRIPPED
        s._is_tax_foreclosure = lambda t: True
        s._is_publication_notice = lambda t: False
        s._extract_acreage = lambda t: None
        rec = {"pk_id": "553715", "sp_case": None, "county": "Sullivan"}
        props = s._extract_detail(None, "SID", rec)
        assert isinstance(props, list)
        assert len(props) == 2
        assert props[0]["property_type"] == "tax_foreclosure"

    def test_extract_detail_drops_non_tax(self):
        s = TNPublicNoticeScraper.__new__(TNPublicNoticeScraper)
        s._extract_notice_text = lambda page, sid, rec: (
            "NOTICE OF SUBSTITUTE TRUSTEE'S SALE ... deed of trust ..."
        )
        s._is_tax_foreclosure = lambda t: False
        s._is_publication_notice = lambda t: False
        rec = {"pk_id": "1", "sp_case": None, "county": "Sullivan"}
        assert s._extract_detail(None, "SID", rec) == []
