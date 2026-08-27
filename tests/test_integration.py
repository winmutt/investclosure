"""End-to-end integration tests for every pipeline scraper.

These tests exercise the REAL ingestion path used by ``python3 -m scraper``:

    scraper.run()  ->  run_scraper(conn, name, cls)  ->  insert_property()  (dedup)

without touching the network: each scraper's ``scrape()`` is monkeypatched to
return a small, realistic batch of ``PropertyData`` records (an in-county parcel
with known acreage, an in-county parcel with *unknown* acreage, and an
out-of-county parcel). The batch is fed through the genuine ``run_scraper``
function so the actual county/acreage filtering, dedup, scrape-run logging, and
DB persistence are all validated.

A second, gated set of tests (``INVESTCLOSURE_LIVE_TESTS=1``) runs the scrapers
for real against their live sources and asserts non-empty results.

Covered scrapers (the ones wired into ``SCRAPER_MODULES``):
    kania_law, zls_nc, newspaper_notices, buncombe_tax,
    ncforeclosures, ganotices, tnforeclosures
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.config import config
from scraper.db import _ensure_db
from scraper import run as run_mod
from scraper import nc_gis_lookup
from scraper.run import run_scraper

from scraper.kania_law import KaniaLawScraper
from scraper.zls_nc import ZLSNCScraper
from scraper.newspaper_notices import NewspaperNoticesScraper
from scraper.buncombe_tax import BuncombeTaxScraper
from scraper.ncforeclosures import NCForeclosureScraper
from scraper.ganotices import GanoticesScraper
from scraper.tnforeclosures import TNForeclosureScraper

# ---------------------------------------------------------------------------
# Registry: every scraper wired into `python3 -m scraper`
# ---------------------------------------------------------------------------
SCRAPERS = {
    "kania_law":        {"cls": KaniaLawScraper,        "state": "NC", "in": "Ashe",         "out": "Wake"},
    "zls_nc":           {"cls": ZLSNCScraper,           "state": "NC", "in": "Ashe",         "out": "Wake"},
    "newspaper_notices": {"cls": NewspaperNoticesScraper, "state": "NC", "in": "Transylvania", "out": "Wake"},
    "buncombe_tax":     {"cls": BuncombeTaxScraper,     "state": "NC", "in": "Buncombe",     "out": "Wake"},
    "ncforeclosures":   {"cls": NCForeclosureScraper,   "state": "NC", "in": "Ashe",         "out": "Wake"},
    "ganotices":        {"cls": GanoticesScraper,       "state": "GA", "in": "Fannin",       "out": "Fulton"},
    "tnforeclosures":   {"cls": "tnforeclosures",       "state": "TN", "in": "Sevier",       "out": "Davidson"},
}

# How many records survive each scraper's real filter (run()):
#   - BaseScraper subclasses (kania/zls/buncombe) have NO run-level filter.
#   - newspaper_notices / ncforeclosures / ganotices / tnforeclosures filter by
#     county (dropping the out-of-county record).
FILTER_EXPECT = {
    "kania_law": 3, "zls_nc": 3, "buncombe_tax": 3,
    "newspaper_notices": 2, "ncforeclosures": 2, "ganotices": 2, "tnforeclosures": 2,
}
# How many records get inserted by run_scraper (tnforeclosures bypasses its
# filter inside run_scraper, using scrape_with_enrichment directly).
INGEST_EXPECT = dict(FILTER_EXPECT)
INGEST_EXPECT["tnforeclosures"] = 3

LIVE = os.environ.get("INVESTCLOSURE_LIVE_TESTS") == "1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def conn(tmp_path):
    """Point the config at a temp DB and return a fresh connection."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config.db_path = data_dir / "integration.db"
    return _ensure_db(config.db_path)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------
def _base(name: str, county: str, acres, pid: str) -> dict:
    return {
        "source_listing_id": f"{name}-{pid}",
        "url": f"https://example.com/{name}/{pid}",
        "address": f"{pid} Main St",
        "city": "Town",
        "county": county,
        "state": SCRAPERS[name]["state"],
        "zip_code": "00000",
        "latitude": None,
        "longitude": None,
        "price": 250000.0,
        "acres": acres,
        "description": f"{name} property {pid}",
        "property_type": "foreclosure",
        "auction_date": "2026-09-01",
        "close_date": None,
        "upset_bid": None,
        "foreclosure_key": None,
        "parcel_number": f"PARCEL-{pid}",
        "deed_book": None,
        "court_case": None,
        "initial_auction_date": "2026-09-01",
        "upset_bid_end": None,
        "google_maps_url": None,
        "google_maps_topo_url": None,
        "gis_url": None,
        "elevation_ft": None,
        "parcel_screenshot": None,
        "raw_source_text": f"raw {name} {pid}",
        "raw_paragraph": None,
        "extracted_pin": None,
        "extracted_deed_plat": None,
    }


def _raw_props(name: str) -> list[dict]:
    s = SCRAPERS[name]
    return [
        _base(name, s["in"], 25.0, "known"),    # in-county, known acreage
        _base(name, s["in"], None, "unknown"),  # in-county, UNKNOWN acreage
        _base(name, s["out"], 25.0, "out"),     # out-of-county
    ]


def _scraper_class(name):
    if name == "tnforeclosures":
        return TNForeclosureScraper
    return SCRAPERS[name]["cls"]


def _run_module_class(name):
    """What run_scraper receives as scraper_class (mirrors run.py)."""
    return "tnforeclosures" if name == "tnforeclosures" else SCRAPERS[name]["cls"]


# ---------------------------------------------------------------------------
# Offline pipeline runner: drives the REAL run_scraper with a mocked fetch
# ---------------------------------------------------------------------------
class _OfflineRunner:
    def __init__(self, conn, name, raw):
        self.conn = conn
        self.name = name
        self.raw = raw
        self._patchers = []

    def __enter__(self):
        p = [
            patch.object(nc_gis_lookup, "enrich_properties", return_value={"enriched": 0}),
            patch.object(run_mod, "_is_scraper_disabled", return_value=False),
            patch.object(run_mod, "_reset_failure_counter", return_value=None),
            patch.object(run_mod, "_inc_failure_counter", return_value=None),
        ]
        if self.name == "tnforeclosures":
            p.append(patch.object(run_mod, "scrape_with_enrichment",
                                  return_value=self.raw))
        else:
            p.append(patch.object(self._run_module_class(self.name), "scrape",
                                  return_value=self.raw))
        for x in p:
            x.start()
        self._patchers = p
        return run_scraper(self.conn, self.name, self._run_module_class(self.name))

    def _run_module_class(self, name):
        return _run_module_class(name)

    def __exit__(self, *exc):
        for x in self._patchers:
            x.stop()


# ===========================================================================
# 1. Pipeline integration: real run_scraper + DB insert + dedup
# ===========================================================================
@pytest.mark.parametrize("name", list(SCRAPERS))
def test_pipeline_ingestion(conn, name):
    raw = _raw_props(name)
    expected = INGEST_EXPECT[name]

    with _OfflineRunner(conn, name, raw) as result:
        assert result["scraper"] == name
        assert result["found"] == expected, result
        assert result["new"] == expected, result

    # DB reflects exactly `expected` active rows.
    active = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE status='active'"
    ).fetchone()[0]
    assert active == expected

    # The unknown-acreage record was NOT filtered out (the core rule).
    null_ct = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE acres IS NULL"
    ).fetchone()[0]
    assert null_ct == 1

    # Dedup: a second identical run produces 0 new, `expected` duplicates.
    with _OfflineRunner(conn, name, raw) as result2:
        assert result2["new"] == 0
        assert result2["duplicates"] == expected, result2

    # No duplicate rows leaked into the table.
    active2 = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE status='active'"
    ).fetchone()[0]
    assert active2 == expected

    # Exactly one row carries the unknown-acreage marker.
    null_ct2 = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE acres IS NULL"
    ).fetchone()[0]
    assert null_ct2 == 1


# ===========================================================================
# 2. Filter integration: real scraper.run() applies county + acreage rules
# ===========================================================================
@pytest.mark.parametrize("name", list(SCRAPERS))
def test_filter(conn, name):
    raw = _raw_props(name)
    cls = _scraper_class(name)
    scraper = cls()

    extra = []
    if name == "ncforeclosures":
        # _enrich_acres hits NC OneMap; mock it for an offline, deterministic run.
        extra.append(patch.object(cls, "_enrich_acres", return_value=raw))
    # ganotices/tnforeclosures run() may reference the disabled-check indirectly;
    # harmless either way, but keep the real path otherwise.
    for e in extra:
        e.start()
    try:
        with patch.object(scraper, "scrape", return_value=raw):
            out = scraper.run()
    finally:
        for e in extra:
            e.stop()

    assert isinstance(out, list)
    assert len(out) == FILTER_EXPECT[name], (name, out)

    # Unknown-acreage record is always retained by every scraper.
    assert any(p.get("acres") is None for p in out), name

    # Out-of-county record is dropped iff the scraper filters by county.
    out_present = any(
        p.get("county", "").lower() == SCRAPERS[name]["out"].lower() for p in out
    )
    if FILTER_EXPECT[name] == 2:
        assert not out_present, name
    else:
        assert out_present, name


# ===========================================================================
# 3. Live end-to-end (gated): real fetch + real ingestion
# ===========================================================================
@pytest.mark.parametrize("name", list(SCRAPERS))
def test_live_end_to_end(conn, name):
    if not LIVE:
        pytest.skip("set INVESTCLOSURE_LIVE_TESTS=1 to run live E2E scrapers")
    scraper_class = _run_module_class(name)
    result = run_scraper(conn, name, scraper_class)
    assert result.get("found", 0) > 0, result
    assert result.get("error") is None, result
    # Ingestion persisted rows.
    active = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE status='active'"
    ).fetchone()[0]
    assert active > 0
