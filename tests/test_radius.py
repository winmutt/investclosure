"""Distance qualification: ground at or above MIN_ELEVATION **and** within
``config.RADIUS_MILES`` of the origin address (2026-11-17).

The county lists were drawn from a loose "mountains within reach of Atlanta"
notion and never enforced, so parcels 250 miles away sat in the inventory.
These tests pin the enforced rule: the haversine from the origin, the USGS
county table (``scraper/county_radius.json``) as the fallback for rows with
no pin, the boundary-versus-centre distinction that keeps counties like
Cleveland NC whose edge reaches inside, the audited reap, and the fact that
an undecidable row is kept rather than guessed.
"""
from __future__ import annotations

import json
import os

import pytest

from scraper import db as scraper_db
from scraper import radius
from scraper.config import config

TABLE_PATH = os.path.join(os.path.dirname(radius.__file__),
                          "county_radius.json")


@pytest.fixture()
def conn(tmp_path):
    return scraper_db._ensure_db(tmp_path / "radius.db")


def _insert(conn, **kw):
    base = dict(
        source="zillow", source_listing_id="z:1", url="http://x",
        address="1 Ridge Rd", city=None, county="haywood", state="NC",
        zip_code=None, latitude=35.6, longitude=-82.9, price_cents=100_000,
        acres=10.0, description="land", property_type="land",
        parcel_number=None, court_case=None,
    )
    base.update(kw)
    if "source_listing_id" not in kw:
        n = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
        base["source_listing_id"] = f"z:{n + 1}"
    _status, row = scraper_db.insert_property(conn, **base)
    return row["id"]


class TestCountyKey:
    """config.py, the scrapers and USGS spell counties three different ways."""

    @pytest.mark.parametrize("a,b", [
        ("Van Buren", "van_buren"),
        ("van_buren", "VAN BUREN"),
        ("St. Clair", "st clair"),
        ("st.clair", "St Clair"),
        ("Mcdowell", "mcdowell"),
    ])
    def test_equivalent_spellings(self, a, b):
        assert radius.county_key(a) == radius.county_key(b)

    def test_finds_the_underscored_config_form(self):
        """The bug this rule exposed: config says van_buren, USGS says Van
        Buren, and a naive lookup missed the county entirely."""
        rec = radius.county_record("TN", "van_buren")
        assert rec is not None
        assert rec["county"] == "Van Buren"


class TestMilesFromOrigin:
    def test_origin_itself_is_zero(self):
        assert radius.miles_from_origin(config.ORIGIN_LAT,
                                        config.ORIGIN_LON) == pytest.approx(
            0.0, abs=1e-6)

    def test_one_degree_north_is_about_69_miles(self):
        d = radius.miles_from_origin(config.ORIGIN_LAT + 1.0,
                                     config.ORIGIN_LON)
        assert d == pytest.approx(69.1, abs=0.5)

    def test_missing_pin_returns_none(self):
        assert radius.miles_from_origin(None, -83.0) is None
        assert radius.miles_from_origin(35.0, None) is None
        assert radius.miles_from_origin("not a number", -83.0) is None

    def test_asheville_distance_matches_the_usgs_neighbourhood(self):
        """Sanity anchor: Asheville sits about 30 mi farther out than the
        nearest point of the county it is in."""
        d = radius.miles_from_origin(35.595, -82.551)
        assert d == pytest.approx(153, abs=6)
        assert d > radius.county_record("NC", "haywood")["boundary_mi"]


class TestCountyVerdict:
    """Real USGS figures, so a silent change in the table fails loudly."""

    def test_county_inside_is_in(self):
        verdict, mi = radius.county_verdict("NC", "Haywood")
        assert verdict == radius.IN
        assert mi == pytest.approx(122.7, abs=0.1)

    def test_county_entirely_outside_is_out_even_unmeasured(self):
        """Knox KY was never measured against the circle — its bounding box
        lower bound (187.2) already exceeds 180, which proves 'outside'."""
        rec = radius.county_record("KY", "knox")
        assert rec["measured"] is False
        verdict, mi = radius.county_verdict("KY", "Knox")
        assert verdict == radius.OUT
        assert mi == pytest.approx(187.2, abs=0.1)

    def test_boundary_not_centre_is_the_test(self):
        """Cleveland NC: nearest ground 170.9 mi, centre 186.5 mi. A
        centre-point rule would drop a county that genuinely holds land
        inside the radius."""
        rec = radius.county_record("NC", "cleveland")
        assert rec["boundary_mi"] < config.RADIUS_MILES < rec["centroid_mi"]
        assert radius.county_verdict("NC", "Cleveland")[0] == radius.IN

    def test_origin_county_is_in(self):
        assert radius.county_record("GA", "cobb")["boundary_mi"] == 0.0
        assert radius.county_verdict("GA", "Cobb")[0] == radius.IN

    def test_exact_threshold_is_in(self):
        """McCreary KY's nearest point measures exactly 180.0."""
        assert radius.county_record("KY", "mccreary")["boundary_mi"] == 180.0
        assert radius.county_verdict("KY", "McCreary")[0] == radius.IN

    def test_unknown_county_is_unknown_not_out(self):
        verdict, mi = radius.county_verdict("ZZ", "Nowhere")
        assert verdict == radius.UNKNOWN
        assert mi is None

    def test_raising_the_threshold_past_the_table_reports_unknown(self):
        """Unmeasured counties carry a lower bound, not a distance: above the
        radius the table was built for, they can no longer be cleared."""
        # Watauga: lower bound 203.1, unmeasured. Still out at 200...
        assert radius.county_verdict("NC", "Watauga", radius=200.0)[0] \
            == radius.OUT
        # ...undecidable at 250, which demands geometry we never measured.
        assert radius.county_verdict("NC", "Watauga", radius=250.0)[0] \
            == radius.UNKNOWN

    def test_disabled_rule_admits_everything(self, monkeypatch):
        monkeypatch.setattr(config, "RADIUS_MILES", 0.0)
        assert radius.enabled() is False
        assert radius.county_verdict("KY", "Perry") == (radius.IN, None)


class TestParcelVerdict:
    def test_pin_decides_it(self):
        verdict, mi = radius.parcel_verdict(config.ORIGIN_LAT,
                                            config.ORIGIN_LON)
        assert verdict == radius.IN and mi < 1
        verdict, mi = radius.parcel_verdict(36.2168, -81.6746)   # Boone NC
        assert verdict == radius.OUT and mi > 200

    def test_no_pin_falls_back_to_the_county(self):
        assert radius.parcel_verdict(None, None, "KY", "Knox")[0] \
            == radius.OUT
        assert radius.parcel_verdict(None, None, "NC", "Haywood")[0] \
            == radius.IN
        assert radius.parcel_verdict(None, None, "NC", "Nowhere")[0] \
            == radius.UNKNOWN

    def test_a_pin_always_beats_the_county(self):
        """A Yancey parcel at the county's south edge is inside even though
        part of the county is not."""
        verdict, _ = radius.parcel_verdict(35.68, -82.25, "NC", "Yancey")
        assert verdict == radius.IN


class TestFindOutside:
    def test_separates_pin_and_county_basis(self, conn):
        inside = _insert(conn, county="haywood", state="NC",
                         latitude=35.6, longitude=-82.9)
        far_pin = _insert(conn, county="haywood", state="NC",
                          latitude=36.2168, longitude=-81.6746)
        no_pin_out_county = _insert(conn, county="knox", state="KY",
                                    latitude=None, longitude=None)
        no_pin_in_county = _insert(conn, county="cleveland", state="NC",
                                   latitude=None, longitude=None)
        hits, unknown = radius.find_outside(conn)
        got = {h["id"]: h["basis"] for h in hits}
        assert got == {far_pin: "parcel", no_pin_out_county: "county"}
        assert inside not in got and no_pin_in_county not in got
        assert unknown == 0

    def test_unknown_county_rows_are_reported_and_kept(self, conn):
        _insert(conn, county="nowhere", state="ZZ", latitude=None,
                longitude=None)
        hits, unknown = radius.find_outside(conn)
        assert hits == []
        assert unknown == 1

    def test_archived_rows_are_not_re_evaluated(self, conn):
        pid = _insert(conn, county="knox", state="KY", latitude=None,
                      longitude=None)
        scraper_db.archive_property(conn, pid, "test", archived_by="system")
        assert radius.find_outside(conn)[0] == []

    def test_source_and_min_id_filters(self, conn):
        old = _insert(conn, county="knox", state="KY", latitude=None,
                      longitude=None)
        new = _insert(conn, source="kania_law", county="knox", state="KY",
                      latitude=None, longitude=None)
        assert {h["id"] for h in radius.find_outside(
            conn, source="kania_law")[0]} == {new}
        assert {h["id"] for h in radius.find_outside(
            conn, min_id=old)[0]} == {new}

    def test_disabled_rule_finds_nothing(self, conn, monkeypatch):
        _insert(conn, county="knox", state="KY", latitude=None,
                longitude=None)
        monkeypatch.setattr(config, "RADIUS_MILES", 0.0)
        stats = radius.apply_radius_rule(conn, archive=True)
        assert stats["checked"] is False
        assert stats["outside"] == 0


class TestApplyRadiusRule:
    def test_archives_with_audit_and_reason(self, conn):
        pid = _insert(conn, county="ashe", state="NC", latitude=36.43,
                      longitude=-81.5)          # ~235 mi
        stats = radius.apply_radius_rule(conn)
        assert stats["archived"] == 1
        row = conn.execute("SELECT status, archive_reason, archived_by, "
                           "archived_at FROM properties WHERE id=?",
                           (pid,)).fetchone()
        assert row[0] == "archived"
        assert row[1].startswith("outside_radius (180 mi)")
        assert row[2] == "system" and row[3]
        audits = conn.execute(
            "SELECT action, reason, actor FROM audit_log WHERE property_id=?",
            (pid,)).fetchall()
        assert ("archive", row[1], "system") in [tuple(a) for a in audits]

    def test_county_basis_reason_says_so(self, conn):
        pid = _insert(conn, county="knox", state="KY", latitude=None,
                      longitude=None)
        radius.apply_radius_rule(conn)
        reason = conn.execute("SELECT archive_reason FROM properties "
                              "WHERE id=?", (pid,)).fetchone()[0]
        assert "county entirely outside" in reason

    def test_dry_run_changes_nothing(self, conn):
        pid = _insert(conn, county="ashe", state="NC", latitude=36.43,
                      longitude=-81.5)
        stats = radius.apply_radius_rule(conn, archive=False)
        assert stats["outside"] == 1 and stats["archived"] == 0
        assert conn.execute("SELECT status FROM properties WHERE id=?",
                            (pid,)).fetchone()[0] == "active"

    def test_reap_is_reversible_by_one_pattern(self, conn):
        """The documented reversal recipe has to match every reason variant."""
        ids = [_insert(conn, county="ashe", state="NC", latitude=36.43,
                       longitude=-81.5),
               _insert(conn, county="knox", state="KY", latitude=None,
                       longitude=None)]
        radius.apply_radius_rule(conn)
        assert len(ids) == 2
        conn.execute("UPDATE properties SET status='active', archived_at=NULL,"
                     " archive_reason=NULL WHERE archive_reason LIKE "
                     "'outside_radius%'")
        conn.commit()
        left = conn.execute("SELECT COUNT(*) FROM properties WHERE "
                            "status='archived'").fetchone()[0]
        assert left == 0

    def test_radius_override_is_recorded(self, conn, monkeypatch):
        """--radius evaluates against another threshold and the reason says
        which one was used, so a what-if reap is never ambiguous."""
        beyond = _insert(conn, county="ashe", state="NC", latitude=36.6,
                         longitude=-81.0)             # 263 mi
        inside_250 = _insert(conn, county="ashe", state="NC", latitude=36.43,
                             longitude=-81.5)         # 235 mi
        monkeypatch.setattr(config, "RADIUS_MILES", 250.0)
        stats = radius.apply_radius_rule(conn)
        assert stats["outside"] == 1
        reason = conn.execute(
            "SELECT archive_reason FROM properties WHERE id=?",
            (beyond,)).fetchone()[0]
        assert reason.startswith("outside_radius (250 mi)")
        assert conn.execute("SELECT status FROM properties WHERE id=?",
                            (inside_250,)).fetchone()[0] == "active"


class TestCountyTable:
    """The committed USGS extract, so a bad regeneration is caught."""

    @staticmethod
    def _table() -> dict:
        with open(TABLE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def test_file_is_present_and_complete(self):
        table = self._table()
        assert table["search_radius_mi"] == 180.0
        assert set(table["counties"]) >= {
            f"{s}/haywood" for s in ("NC",)}
        by_state = {}
        for key, rec in table["counties"].items():
            st = key.split("/")[0]
            by_state[st] = by_state.get(st, 0) + 1
        # AL 67, GA 159, KY 120, NC 100, SC 46, TN 95
        assert by_state == {"AL": 67, "GA": 159, "KY": 120, "NC": 100,
                            "SC": 46, "TN": 95}

    def test_every_record_carries_what_radius_needs(self):
        for key, rec in self._table()["counties"].items():
            assert rec["county"], key
            assert isinstance(rec["boundary_mi"], (int, float)), key
            assert isinstance(rec["measured"], bool), key

    def test_inside_count_matches_the_usgs_extract(self):
        table = self._table()
        inside = [k for k, v in table["counties"].items()
                  if v["measured"] and v["boundary_mi"] <= 180.0]
        assert len(inside) == 271

    def test_missing_table_degrades_to_unknown_not_out(self, tmp_path):
        """An outage or a forgotten fetcher must never mass-archive."""
        assert radius.county_verdict(
            "NC", "Haywood", path=str(tmp_path / "absent.json"))[0] \
            == radius.UNKNOWN
