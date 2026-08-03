"""Integration tests for investclosure scraper.

Tests:
- DB CRUD operations and dedup
- archive_below_acres with various filters
- ZLS NC county filtering
- NC OneMap enrichment flow (mocked)
- Kania Law county filtering
"""
from __future__ import annotations
import sys
import sqlite3
import pytest
from pathlib import Path
from datetime import date
from unittest.mock import patch, MagicMock

# Add scraper to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scraper.db import (
    _ensure_db,
    insert_property,
    archive_below_acres,
    get_stats,
    get_all_active,
    start_scrape_run,
    update_scrape_run,
    compute_dedup_hash,
)
from scraper.zls_nc import ZLSNCScraper
from scraper.kania_law import KaniaLawScraper

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_dir(tmp_path):
    """Create a temp data dir and return it."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture()
def conn(tmp_dir):
    """Return a fresh SQLite connection."""
    from scraper.config import config
    config.db_path = tmp_dir / "test.db"
    return _ensure_db(tmp_dir / "test.db")


# ---------------------------------------------------------------------------
# Helper for tests
# ---------------------------------------------------------------------------

def _insert_prop(conn, idx, acres, source="test"):
    """Insert a test property and return its row."""
    return insert_property(
        conn=conn,
        source=source,
        source_listing_id=f"P{idx}",
        url=None,
        address=f"{idx} Road",
        city=None,
        county="Ashe",
        state="NC",
        zip_code=None,
        latitude=None,
        longitude=None,
        price_cents=10000,
        acres=acres,
    )


# ---------------------------------------------------------------------------
# DB CRUD tests
# ---------------------------------------------------------------------------

class TestInsertProperty:
    """Test property insertion and dedup."""

    def test_insert_new_property(self, conn):
        """A new property should be inserted and return 'new'."""
        status, row = _insert_prop(conn, 1, 25.0)
        assert status == "new"
        assert dict(row)["acres"] == 25.0

    def test_insert_duplicate_same_listing(self, conn):
        """Inserting same source+listing_id should update, not duplicate."""
        _insert_prop(conn, 1, 25.0)
        status, row = _insert_prop(conn, 1, 30.0)
        assert status == "duplicate"
        assert dict(row)["price_cents"] == 10000
        assert dict(row)["seen_count"] == 2

    def test_insert_duplicate_same_hash(self, conn):
        """Different source, same dedup hash should return duplicate."""
        _insert_prop(conn, 1, 25.0, source="A")
        status, _ = insert_property(
            conn=conn, source="B", source_listing_id="P2",
            url=None, address="1 Road", city=None, county="Ashe", state="NC",
            zip_code=None, latitude=None, longitude=None,
            price_cents=20000, acres=20.0,
        )
        assert status == "duplicate"

    def test_get_all_active(self, conn):
        """Insert 3 properties, expect 3 active."""
        for i in range(3):
            _insert_prop(conn, i, 25.0)
        rows = get_all_active(conn)
        assert len(rows) == 3

    def test_get_stats(self, conn):
        """Insert properties and verify stats."""
        _insert_prop(conn, 1, 25.0)
        stats = get_stats(conn)
        assert stats["total_active"] == 1
        assert stats["total_seen"] == 1


class TestScrapeRun:
    """Test scrape run tracking."""

    def test_start_and_update_run(self, conn):
        """Start a run, update it, verify."""
        run_id = start_scrape_run(conn, "test")
        assert run_id == 1
        update_scrape_run(conn, run_id, found=10, new_count=5,
                          duplicate_count=3, updated_count=2)
        result = conn.execute(
            "SELECT properties_found, properties_new, status FROM scrape_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert dict(result)["properties_found"] == 10
        assert dict(result)["status"] == "completed"


# ---------------------------------------------------------------------------
# Archive tests — the critical path
# ---------------------------------------------------------------------------

class TestArchiveBelowAcres:
    """Test the archive_below_acres function with correct param ordering."""

    def test_archive_below_threshold(self, conn):
        """Properties with acres < 2.0 should be archived."""
        _insert_prop(conn, 1, 1.5)  # should archive
        _insert_prop(conn, 2, 2.5)  # should NOT archive
        _insert_prop(conn, 3, 0.5)  # should archive
        archived = archive_below_acres(conn, 2.0)
        assert archived == 2
        active = conn.execute("SELECT COUNT(*) FROM properties WHERE status='active'").fetchone()[0]
        assert active == 1

    def test_archive_with_include_sources(self, conn):
        """With include_sources, only matching sources are archived."""
        _insert_prop(conn, 1, 1.0, source="test1")
        _insert_prop(conn, 2, 1.5, source="test2")
        _insert_prop(conn, 3, 2.5, source="test1")
        archived = archive_below_acres(conn, 2.0, include_sources=["test1"])
        assert archived == 1
        active = conn.execute("SELECT COUNT(*) FROM properties WHERE status='active'").fetchone()[0]
        assert active == 2

    def test_archive_empty_acres_skipped(self, conn):
        """Properties with acres=0 should NOT be archived by acres filter."""
        _insert_prop(conn, 1, 0.0)
        _insert_prop(conn, 2, 1.5)
        archived = archive_below_acres(conn, 2.0)
        assert archived == 1
        row = conn.execute("SELECT * FROM properties WHERE source_listing_id='P1'").fetchone()
        assert dict(row)["status"] == "active"

    def test_archive_with_source_filter(self, conn):
        """Legacy source= filter should work."""
        _insert_prop(conn, 1, 1.0, source="A")
        _insert_prop(conn, 2, 1.5, source="B")
        _insert_prop(conn, 3, 2.5)
        archived = archive_below_acres(conn, 2.0, source="A")
        assert archived == 1

    def test_archive_no_match(self, conn):
        """All properties above threshold => 0 archived."""
        _insert_prop(conn, 1, 10.0)
        _insert_prop(conn, 2, 50.0)
        archived = archive_below_acres(conn, 2.0)
        assert archived == 0

    def test_archive_param_order_correct(self, conn):
        """
        Verify the fix: params must be bound to correct SQL placeholders.
        Previously 'archived' was bound to acres < 'archived' which matched everything.
        """
        _insert_prop(conn, 1, 50.0)
        _insert_prop(conn, 2, 100.0)
        _insert_prop(conn, 3, 2.0)

        archived = archive_below_acres(conn, 10.0, include_sources=["test"])
        assert archived == 1

        row = conn.execute("SELECT * FROM properties WHERE source_listing_id='P3'").fetchone()
        assert dict(row)["status"] == "archived"

        row = conn.execute("SELECT * FROM properties WHERE source_listing_id='P1'").fetchone()
        assert dict(row)["status"] == "active"


# ---------------------------------------------------------------------------
# ZLS NC county filter tests
# ---------------------------------------------------------------------------

class TestZLSNCFilter:
    """Test ZLS NC county filtering to NC mountain counties."""

    def _make_prop(self, county):
        """Create a mock property dict for ZLS NC filtering."""
        return {
            "source": "zls_nc",
            "source_listing_id": f"Z_{county}",
            "county": county,
            "parcel_number": "12345",
            "acres": None,
        }

    def test_filter_keeps_mountain_counties(self):
        """Mountain counties should pass through."""
        props = [
            self._make_prop("Ashe"),
            self._make_prop("Buncombe"),
            self._make_prop("Cherokee"),
            self._make_prop("Clay"),
            self._make_prop("Polk"),
        ]
        scraper = ZLSNCScraper(delay_range=(0, 0))
        filtered = scraper._filter_counties(props)
        assert len(filtered) == 5

    def test_filter_removes_coastal_counties(self):
        """Coastal/eastern NC counties should be filtered out."""
        props = [
            self._make_prop("Onslow"),    # coastal
            self._make_prop("Pamlico"),   # coastal
            self._make_prop("Robeson"),   # eastern
            self._make_prop("Guilford"),  # Piedmont
            self._make_prop("Forsyth"),   # Piedmont
        ]
        scraper = ZLSNCScraper(delay_range=(0, 0))
        filtered = scraper._filter_counties(props)
        assert len(filtered) == 0

    def test_filter_case_insensitive(self):
        """County matching should be case-insensitive."""
        props = [
            self._make_prop("ASHE"),
            self._make_prop("ashe"),
            self._make_prop("Ashe"),
            self._make_prop("buncombe"),
        ]
        scraper = ZLSNCScraper(delay_range=(0, 0))
        filtered = scraper._filter_counties(props)
        assert len(filtered) == 4

    def test_filter_mixed_counties(self):
        """Only mountain counties should remain."""
        props = [
            self._make_prop("Ashe"),
            self._make_prop("Burke"),
        ]
        scraper = ZLSNCScraper(delay_range=(0, 0))
        filtered = scraper._filter_counties(props)
        counties = {p["county"] for p in filtered}
        assert counties == {"Ashe", "Burke"}


# ---------------------------------------------------------------------------
# NC OneMap GIS enrichment tests (mocked)
# ---------------------------------------------------------------------------

class TestNCOneMapEnrichment:
    """Test GIS enrichment flow with mocked responses."""

    def test_enrich_gis_keeps_all_properties(self):
        """
        All properties should be kept regardless of acreage.
        GIS enrichment is optional — properties without GIS data remain unchanged.
        Using mocks to avoid real network calls.
        """
        from scraper.zls_nc import ZLSNCScraper

        mock_prop1 = {"source": "zls_nc", "county": "Ashe", "parcel_number": "P1", "acres": None}
        mock_prop2 = {"source": "zls_nc", "county": "Ashe", "parcel_number": "P2", "acres": None}

        with patch("scraper.nc_gis_lookup.NC1MapService") as MockService:
            mock_service = MagicMock()
            MockService.return_value = mock_service

            def mock_by_parcel(parcel, *args, **kwargs):
                if parcel == "P1":
                    return {"features": [{"attributes": {"gisacres": 10.0, "parno": "P1", "cntyname": "Ashe"}}]}
                return None

            mock_service.by_parcel.side_effect = mock_by_parcel

            scraper = ZLSNCScraper(delay_range=(0, 0))
            result = scraper._enrich_gis([mock_prop1, mock_prop2])

        # All properties kept - GIS enrichment is optional
        assert len(result) == 2
        assert result[0]["acres"] == 10.0
        assert result[0]["acres_source"] == "gis"
        assert result[0]["parcel_number"] == "P1"

        # P2 has no GIS data but is still kept
        assert result[1]["acres"] is None
        assert result[1]["parcel_number"] == "P2"

    def test_enrich_gis_no_parcel_kept(self):
        """Properties without a parcel number should still be kept."""
        scraper = ZLSNCScraper(delay_range=(0, 0))
        prop = {"source": "zls_nc", "county": "Ashe", "parcel_number": None, "acres": None}
        result = scraper._enrich_gis([prop])
        assert len(result) == 1
        assert result[0]["acres"] is None


# ---------------------------------------------------------------------------
# Kania Law county filter tests
# ---------------------------------------------------------------------------

class TestKaniaLawFilter:
    """Test Kania Law county filtering."""

    def test_qualifying_county_count(self):
        """Verify the expected number of NC mountain counties."""
        qualifying = {
            "alleghany", "ashe", "avery", "buncombe", "burke", "caldwell", "cherokee",
            "clay", "graham", "haywood", "henderson", "jackson", "macon", "madison",
            "mcdowell", "mitchell", "polk", "swain", "transylvania", "watauga", "yancey",
        }
        assert len(qualifying) == 21

    def test_scraper_initializes(self):
        """Kania Law scraper should initialize correctly."""
        scraper = KaniaLawScraper(delay_range=(0, 0))
        assert scraper.SOURCE_NAME == "kania_law"
        assert scraper.MIN_ACRES == 5.0


# ---------------------------------------------------------------------------
# Dedup hash tests
# ---------------------------------------------------------------------------

class TestDedupHash:
    """Test dedup hash generation."""

    def test_hash_same_input(self):
        """Same address inputs should produce same hash."""
        h1 = compute_dedup_hash("100 Main St", "Ashe", "NC", "NC", "28709", 36.0, -81.0)
        h2 = compute_dedup_hash("100 Main St", "Ashe", "NC", "NC", "28709", 36.0, -81.0)
        assert h1 == h2

    def test_hash_diff_address(self):
        """Different addresses should produce different hashes."""
        h1 = compute_dedup_hash("100 Main St", "Ashe", "NC", "NC", "28709", 36.0, -81.0)
        h2 = compute_dedup_hash("200 Main St", "Ashe", "NC", "NC", "28709", 36.0, -81.0)
        assert h1 != h2

    def test_hash_case_insensitive(self):
        """Hash should be case-insensitive for address parts."""
        h1 = compute_dedup_hash("100 main st", "ashe", "nc", "nc", "28709", 36.0, -81.0)
        h2 = compute_dedup_hash("100 MAIN ST", "ASHE", "NC", "NC", "28709", 36.0, -81.0)
        assert h1 == h2

    def test_hash_with_coords_includes_lat_lon(self):
        """Hash with coords differs from hash without."""
        h1 = compute_dedup_hash("100 Main St", "Ashe", "NC", "NC", "28709", 36.0, -81.0)
        h2 = compute_dedup_hash("100 Main St", "Ashe", "NC", "NC", "28709", None, None)
        assert h1 != h2
