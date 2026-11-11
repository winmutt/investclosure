"""Per-parcel elevation enrichment + the 1,700 ft parcel rule.

The county lists only encode each county's PEAK elevation, so valley parcels
inside a qualifying county used to pass every filter with NULL elevation.
These tests pin the USGS 3DEP measurement path (batching, split retry, EPQS
fallback), the archive rule, and the stale-run cleanup that unblocks sweeps.
"""
from __future__ import annotations

import pytest

from scraper import db as scraper_db
from scraper import elevation


M_PER_FT = 0.3048


@pytest.fixture()
def conn(tmp_path):
    return scraper_db._ensure_db(tmp_path / "elevation.db")


def _insert(conn, **kw):
    base = dict(
        source="zillow", source_listing_id="z:1", url="http://x",
        address="1 Ridge Rd", city=None, county="macon", state="NC",
        zip_code=None, latitude=35.1, longitude=-83.2, price_cents=100_000,
        acres=10.0, description="land", property_type="land",
        parcel_number=None, court_case=None,
    )
    base.update(kw)
    _status, row = scraper_db.insert_property(conn, **base)
    return row["id"]


def _sample(index, lat, lon, value):
    """One getSamples answer in the shape the service really sends.

    Measured live 2026-11-10: every sample carries the request index as
    ``locationId`` plus the ``location`` it sampled, and the batch comes back
    in an order that need not match the request.
    """
    return {
        "locationId": index,
        "location": {"x": lon, "y": lat,
                     "spatialReference": {"wkid": 4326}},
        "value": value,
        "rasterId": 93006,
        "resolution": 1,
    }


class TestSamplesBatch:
    def test_parses_metres_into_feet_and_nodata(self, monkeypatch):
        seen = {}
        points = [(35.0, -83.0), (35.1, -83.1), (35.2, -83.2)]

        def fake_get(url, params=None):
            seen["url"] = url
            seen["params"] = params
            return {"samples": [
                _sample(0, 35.0, -83.0, "304.8"),      # 1000 ft
                _sample(1, 35.1, -83.1, "NoData"),
                _sample(2, 35.2, -83.2, "0"),
            ]}

        monkeypatch.setattr(elevation, "_get_json", fake_get)
        values = elevation._samples_batch(points)
        assert values == [1000.0, None, 0.0]
        assert "3DEPElevation" in seen["url"]
        assert seen["params"]["geometryType"] == "esriGeometryMultipoint"
        assert '"wkid":4326' in seen["params"]["geometry"]

    def test_short_response_is_padded_with_none(self, monkeypatch):
        monkeypatch.setattr(elevation, "_get_json",
                            lambda url, params=None: {"samples": [
                                _sample(0, 1, 2, "304.8")]})
        assert elevation._samples_batch([(1, 2), (3, 4)]) == [1000.0, None]

    def test_failed_request_returns_empty(self, monkeypatch):
        monkeypatch.setattr(elevation, "_get_json", lambda *a, **k: None)
        # A failed request is indistinguishable from NoData: one slot per point.
        assert elevation._samples_batch([(1, 2)]) == [None]


class TestSampleJoining:
    """The mis-assignment bug: values must follow their own point.

    Live 2026-11-10: a 7-point request answered with ``locationId``
    4,3,6,0,1,2,5. Reading the values positionally gave every property some
    other property's elevation — #2913 stored 1,478 ft (archived) while its own
    point measures 2,652 ft.
    """

    POINTS = [(35.76805, -82.26802),    # 0  Mt Mitchell-ish   6,132 ft
              (36.21678, -81.67454),    # 1  Boone-ish        3,218 ft
              (35.43619, -82.54163)]    # 2  Asheville apt    2,145 ft

    def test_out_of_order_response_keeps_each_points_own_value(self,
                                                               monkeypatch):
        meters = [1869.180786133, 980.914978027, 653.795532227]
        # answered 1,0,2 — nothing like the request order
        answered = [_sample(1, self.POINTS[1][0], self.POINTS[1][1],
                            str(meters[1])),
                    _sample(0, self.POINTS[0][0], self.POINTS[0][1],
                            str(meters[0])),
                    _sample(2, self.POINTS[2][0], self.POINTS[2][1],
                            str(meters[2]))]
        monkeypatch.setattr(elevation, "_get_json",
                            lambda url, params=None: {"samples": answered})
        values = elevation._samples_batch(self.POINTS)
        assert [round(v, 1) for v in values] == [6132.5, 3218.2, 2145.0]

    def test_mismatched_echo_is_dropped_for_epqs_retry(self, monkeypatch):
        # the sample claims to be point 0 but reports a coordinate 0.01 deg away
        monkeypatch.setattr(elevation, "_get_json", lambda url, params=None: {
            "samples": [{"locationId": 0,
                         "location": {"x": -82.26, "y": 35.77},
                         "value": "1869.18"}]})
        assert elevation._samples_batch(self.POINTS[:1]) == [None]

    def test_sample_without_location_id_is_dropped(self, monkeypatch):
        monkeypatch.setattr(elevation, "_get_json", lambda url, params=None: {
            "samples": [{"location": {"x": -82.26802, "y": 35.76805},
                         "value": "1869.18"}]})
        assert elevation._samples_batch(self.POINTS[:1]) == [None]

    def test_location_id_outside_the_request_is_dropped(self, monkeypatch):
        monkeypatch.setattr(elevation, "_get_json", lambda url, params=None: {
            "samples": [_sample(7, 35.76805, -82.26802, "1869.18")]})
        assert elevation._samples_batch(self.POINTS) == [None, None, None]

    def test_elevations_end_to_end_order_and_values(self, monkeypatch):
        meters = [1869.180786133, 980.914978027, 653.795532227]

        def fake_get(url, params=None):
            return {"samples": [
                _sample(1, self.POINTS[1][0], self.POINTS[1][1],
                        str(meters[1])),
                _sample(0, self.POINTS[0][0], self.POINTS[0][1],
                        str(meters[0])),
                # point 2 answered with a foreign coordinate -> EPQS retries
                _sample(2, 34.0, -84.0, str(meters[2])),
            ]}

        monkeypatch.setattr(elevation, "_get_json", fake_get)
        monkeypatch.setattr(elevation, "_sleep", lambda: None)
        monkeypatch.setattr(elevation, "_epqs_point",
                            lambda lat, lon: 500.0)
        values = elevation.elevations(self.POINTS)
        assert [round(v, 1) for v in values] == [6132.5, 3218.2, 500.0]


class TestEpqsPoint:
    def test_answers_in_feet_for_the_echoed_point(self, monkeypatch):
        monkeypatch.setattr(elevation, "_get_json", lambda url, params=None: {
            "location": {"x": "-82.54163", "y": "35.43619"},
            "locationId": 0, "value": "2144.9985339516306"})
        assert elevation._epqs_point(35.43619, -82.54163) == \
            pytest.approx(2144.9985, abs=1e-3)

    def test_foreign_echo_is_refused(self, monkeypatch):
        monkeypatch.setattr(elevation, "_get_json", lambda url, params=None: {
            "location": {"x": -81.0, "y": 34.0}, "value": "900.0"})
        assert elevation._epqs_point(35.43619, -82.54163) is None

    def test_no_answer(self, monkeypatch):
        monkeypatch.setattr(elevation, "_get_json", lambda url, params=None: None)
        assert elevation._epqs_point(35.43619, -82.54163) is None


class TestBatchLimit:
    def test_limit_is_ten_points(self):
        # Measured 2026-11-09: 10 points answer 200, 20 answer 502.
        assert elevation.MAX_POINTS_PER_REQUEST == 10

    def test_elevations_chunks_requests(self, monkeypatch):
        calls = []

        def fake_batch(chunk):
            calls.append(len(chunk))
            return [1000.0] * len(chunk)

        monkeypatch.setattr(elevation, "_samples_batch", fake_batch)
        monkeypatch.setattr(elevation, "_sleep", lambda: None)
        values = elevation.elevations([(35.0 + i / 100, -83.0)
                                       for i in range(23)])
        assert calls == [10, 10, 3]
        assert values == [1000.0] * 23


class TestSplitRetryAndFallback:
    def test_failed_batch_splits_and_keeps_order(self, monkeypatch):
        def fake_batch(chunk):
            # Every multi-point request "fails"; single points succeed.
            if len(chunk) > 1:
                return []
            (lat, lon), = chunk
            return [lat * 100]

        monkeypatch.setattr(elevation, "_samples_batch", fake_batch)
        monkeypatch.setattr(elevation, "_sleep", lambda: None)
        points = [(35.01, -83.0), (35.02, -83.0), (35.03, -83.0)]
        assert elevation.elevations(points) == pytest.approx(
            [3501.0, 3502.0, 3503.0])

    def test_single_point_failure_falls_back_to_epqs(self, monkeypatch):
        monkeypatch.setattr(elevation, "_samples_batch",
                            lambda chunk: [None] * len(chunk))
        monkeypatch.setattr(elevation, "_sleep", lambda: None)
        seen = []

        def fake_epqs(lat, lon):
            seen.append((lat, lon))
            return 1800.0

        monkeypatch.setattr(elevation, "_epqs_point", fake_epqs)
        assert elevation.elevations([(35.0, -83.0)]) == [1800.0]
        assert seen == [(35.0, -83.0)]

    def test_elevation_ft_prefers_samples(self, monkeypatch):
        monkeypatch.setattr(elevation, "_samples_batch",
                            lambda chunk: [1234.5])
        monkeypatch.setattr(elevation, "_sleep", lambda: None)
        monkeypatch.setattr(
            elevation, "_epqs_point",
            lambda *a: pytest.fail("EPQS should not be called"))
        assert elevation.elevation_ft(35.0, -83.0) == 1234.5


class TestEnrichElevations:
    def test_fills_and_archives_below_threshold(self, conn, monkeypatch):
        low = _insert(conn, source_listing_id="z:low")
        high = _insert(conn, source_listing_id="z:high")
        unknown = _insert(conn, source_listing_id="z:unk")
        monkeypatch.setattr(elevation.config, "MIN_ELEVATION", 1700.0)
        monkeypatch.setattr(elevation, "elevations",
                            lambda points: [900.0, 2200.0, None])

        stats = elevation.enrich_elevations(conn=conn)

        assert stats["target"] == 3
        assert stats["measured"] == 2
        assert stats["unknown"] == 1
        assert stats["below"] == 1
        assert stats["archived"] == 1
        assert stats["by_state"]["NC"] == {"measured": 2, "below": 1}

        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, status, elevation_ft FROM properties")}
        assert rows[low]["elevation_ft"] == 900.0
        assert rows[low]["status"] == "archived"
        assert rows[high]["status"] == "active"
        # unknown elevation is never disqualifying
        assert rows[unknown]["status"] == "active"
        assert rows[unknown]["elevation_ft"] is None

    def test_write_false_measures_without_touching_db(self, conn, monkeypatch):
        pid = _insert(conn, source_listing_id="z:dry")
        monkeypatch.setattr(elevation.config, "MIN_ELEVATION", 1700.0)
        monkeypatch.setattr(elevation, "elevations", lambda points: [800.0])

        stats = elevation.enrich_elevations(conn=conn, write=False)

        assert stats["measured"] == 1
        assert stats["below"] == 1
        assert stats["updated"] == 0
        assert stats["archived"] == 0
        row = conn.execute("SELECT status, elevation_ft FROM properties "
                           "WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "active"
        assert row["elevation_ft"] is None

    def test_no_archive_keeps_low_rows(self, conn, monkeypatch):
        pid = _insert(conn, source_listing_id="z:keep")
        monkeypatch.setattr(elevation.config, "MIN_ELEVATION", 1700.0)
        monkeypatch.setattr(elevation, "elevations", lambda points: [500.0])

        stats = elevation.enrich_elevations(conn=conn, archive=False)

        assert stats["updated"] == 1
        assert stats["archived"] == 0
        assert conn.execute("SELECT status FROM properties WHERE id=?",
                            (pid,)).fetchone()["status"] == "active"

    def test_only_missing_skips_measured_rows(self, conn, monkeypatch):
        pid = _insert(conn, source_listing_id="z:done")
        conn.execute("UPDATE properties SET elevation_ft = 2500 WHERE id=?",
                     (pid,))
        conn.commit()
        monkeypatch.setattr(elevation, "elevations",
                            lambda points: pytest.fail("must not measure"))

        stats = elevation.enrich_elevations(conn=conn)

        assert stats["target"] == 0

    def test_min_id_scopes_to_this_run(self, conn, monkeypatch):
        _insert(conn, source_listing_id="z:old")
        new_id = _insert(conn, source_listing_id="z:new")
        monkeypatch.setattr(elevation.config, "MIN_ELEVATION", 1700.0)
        monkeypatch.setattr(elevation, "elevations",
                            lambda points: [2500.0] * len(list(points)))

        stats = elevation.enrich_elevations(conn=conn, min_id=new_id - 1)

        assert stats["target"] == 1
        row = conn.execute("SELECT id FROM properties "
                           "WHERE elevation_ft IS NOT NULL").fetchone()
        assert row["id"] == new_id

    def test_rule_disabled_measures_but_never_archives(self, conn, monkeypatch):
        _insert(conn, source_listing_id="z:low")
        monkeypatch.setattr(elevation.config, "MIN_ELEVATION", 0.0)
        monkeypatch.setattr(elevation, "elevations", lambda points: [500.0])

        stats = elevation.enrich_elevations(conn=conn)

        assert stats["updated"] == 1
        assert stats["archived"] == 0
        assert conn.execute("SELECT COUNT(*) FROM properties "
                            "WHERE status='active'").fetchone()[0] == 1


class TestArchiveBelowElevation:
    def test_archives_and_audits_but_leaves_null_alone(self, conn):
        low = _insert(conn, source_listing_id="z:a")
        high = _insert(conn, source_listing_id="z:b")
        missing = _insert(conn, source_listing_id="z:c")
        conn.execute("UPDATE properties SET elevation_ft=900 WHERE id=?", (low,))
        conn.execute("UPDATE properties SET elevation_ft=2500 WHERE id=?",
                     (high,))
        conn.commit()

        archived = scraper_db.archive_below_elevation(conn, 1700.0)

        assert archived == 1
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, status, archived_by, archive_reason FROM properties")}
        assert rows[low]["status"] == "archived"
        assert rows[low]["archived_by"] == "system"
        assert rows[low]["archive_reason"] == "below_min_elevation (1700 ft)"
        assert rows[high]["status"] == "active"
        assert rows[missing]["status"] == "active"
        audit = conn.execute("SELECT * FROM audit_log WHERE property_id=?",
                             (low,)).fetchall()
        assert len(audit) == 1
        assert audit[0]["actor"] == "system"
        assert audit[0]["reason"] == "below_min_elevation (1700 ft)"

    def test_source_filter_scopes_the_reap(self, conn):
        a = _insert(conn, source="zillow", source_listing_id="z:1")
        b = _insert(conn, source="kania_law", source_listing_id="k:1")
        conn.execute("UPDATE properties SET elevation_ft=900")
        conn.commit()

        assert scraper_db.archive_below_elevation(conn, 1700.0,
                                                  source="zillow") == 1
        rows = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id, status FROM properties")}
        assert rows[a] == "archived"
        assert rows[b] == "active"


class TestFailStaleRuns:
    def test_marks_old_running_runs_failed_only(self, conn):
        old = scraper_db.start_scrape_run(conn, "zillow")
        fresh = scraper_db.start_scrape_run(conn, "zillow")
        conn.execute("UPDATE scrape_runs SET started_at = datetime('now', "
                     "'-5 hours') WHERE id=?", (old,))
        conn.commit()

        assert scraper_db.fail_stale_runs(conn, max_age_hours=3) == 1

        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, status, error_message FROM scrape_runs")}
        assert rows[old]["status"] == "failed"
        assert "stale" in rows[old]["error_message"]
        assert rows[fresh]["status"] == "running"

    def test_noop_when_nothing_is_stale(self, conn):
        scraper_db.start_scrape_run(conn, "zillow")
        assert scraper_db.fail_stale_runs(conn) == 0


class TestConfig:
    def test_default_is_1700ft(self):
        from scraper.config import Config
        assert Config().MIN_ELEVATION == 1700.0

    def test_env_override(self, monkeypatch):
        from scraper.config import Config
        monkeypatch.setenv("INVESTCLOSURE_MIN_ELEVATION", "2000")
        assert Config().MIN_ELEVATION == 2000.0

    def test_zero_disables(self, monkeypatch):
        from scraper.config import Config
        monkeypatch.setenv("INVESTCLOSURE_MIN_ELEVATION", "0")
        assert Config().MIN_ELEVATION == 0.0
