"""Multi-parcel tracts + placeholder-address resolution.

One Zillow listing can span several tax parcels (Jackson #2739: 26.02ac =
7528-56-9321 23.0ac + 7528-56-9996 3.02ac), and vacant-land listings arrive
with a placeholder house number ("0 Leon Barron Rd", "TBD Poplar Grove Road",
"LOT 1 Butternut Rdg") or a whole "street, city, ST zip" line that the OneMap
normalizer used to reduce to "SYLVA, NC 28779". These tests cover the
detection helpers, the owner-group resolver, the multi-pin viewer/detail
plumbing and the link rebuild that must not drop a PIN.
"""
import base64
import sqlite3

import pytest

from scraper.db import _ensure_db, insert_property


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _feat(parno, acres, owner, site="LEON BARRON RD", county="Jackson",
          fips="099"):
    """One OneMap feature in the shape _clean_features consumes."""
    return {
        "attributes": {
            "parno": parno, "altparno": "", "cntyname": county,
            "cntyfips": fips, "gisacres": acres, "siteadd": site,
            "ownname": owner,
        },
        # centroid-free: the resolver keeps the largest parcel's centroid,
        # and None simply means the row carries no coordinates.
        "geometry": None,
    }


GUFFEY = (_feat("7528-56-9321", 23.0, "GUFFEY, FREDERICK RAD ETAL"),
          _feat("7528-56-9996", 3.02, "GUFFEY, FREDERICK RAD ETAL"))
NEIGHBOURS = (_feat("7528-46-9510", 5.5, "ELDERS, RITA B",
                    site="313 LEON BARRON RD"),
              _feat("7528-57-0759", 5.05, "BERNSTEIN, HELENE"))


class _QuerySpy:
    """Stand-in for nc_gis_lookup._nc1map_query recording every WHERE."""

    def __init__(self, features):
        self.features = list(features)
        self.calls = []

    def __call__(self, post_data, timeout=15):
        self.calls.append(post_data.get("where", ""))
        return self.features


# ---------------------------------------------------------------------------
# house-number detection
# ---------------------------------------------------------------------------

class TestHasHouseNumber:
    def test_real_numbers(self):
        from scraper.nc_gis_lookup import has_house_number
        assert has_house_number("1234 Asheville Hwy") is True
        assert has_house_number("12A Main St") is True
        assert has_house_number("123-125 Green St") is True
        assert has_house_number("999 Moody Cove Rd") is True

    def test_zillow_placeholder_tokens(self):
        from scraper.nc_gis_lookup import has_house_number
        for addr in ("0 Leon Barron Rd", "00 US 19 Highway", "000 Hal Dr",
                     "TBD Hart Rd", "TBA Poplar Grove Road",
                     "LOT 6 Chestnut Walk Dr", "NULLD Chestnut Walk Dr",
                     "9999 Us Highway 441 Hwy S", "99999 Morgan Hill Rd",
                     "999999 Harwood Rd #2"):
            assert has_house_number(addr) is False, addr

    def test_street_name_first_is_not_a_number(self):
        from scraper.nc_gis_lookup import has_house_number
        # leading tokens that must not be treated as situs numbers, and must
        # not be stripped out of the street either
        for addr in ("Tusquittee Mountain Rd", "W Emory Rd",
                     "5.93 Indian Creek Rd", "Pin Oak Ln", "Union Ave"):
            assert has_house_number(addr) is False, addr

    def test_empty(self):
        from scraper.nc_gis_lookup import has_house_number
        assert has_house_number(None) is False
        assert has_house_number("") is False


class TestStripPlaceholderHouse:
    def test_strips_tokens_and_lot_numbers(self):
        from scraper.nc_gis_lookup import strip_placeholder_house
        assert strip_placeholder_house("0 Leon Barron Rd") == "Leon Barron Rd"
        assert strip_placeholder_house("TBD Poplar Grove Road") == "Poplar Grove Road"
        assert strip_placeholder_house("LOT 6 Chestnut Walk Dr") == "Chestnut Walk Dr"
        assert strip_placeholder_house("NULLD Chestnut Walk Dr") == "Chestnut Walk Dr"

    def test_keeps_real_addresses_intact(self):
        from scraper.nc_gis_lookup import strip_placeholder_house
        assert strip_placeholder_house("150 Piney Ridge Drive") == "150 Piney Ridge Drive"
        # short-token alternatives must not eat street words
        assert strip_placeholder_house("Pin Oak Ln") == "Pin Oak Ln"
        assert strip_placeholder_house("Union Ave") == "Union Ave"
        assert strip_placeholder_house("Trout Creek Rd") == "Trout Creek Rd"


class TestStreetOnly:
    def test_cuts_city_state_zip(self):
        from scraper.nc_gis_lookup import street_only
        assert street_only("0 Leon Barron Rd, Sylva, NC 28779") == "0 Leon Barron Rd"
        assert street_only("64 Riverview Rd #64, Millers Creek, NC 28651") == "64 Riverview Rd"
        assert street_only("240 Old Stone Dr Lot 47, Crumpler, NC 28617") == "240 Old Stone Dr"

    def test_no_comma_form(self):
        from scraper.nc_gis_lookup import street_only
        assert street_only("150 Piney Ridge Drive") == "150 Piney Ridge Drive"
        assert street_only("Little Glade Creek Rd NC 28743") == "Little Glade Creek Rd"

    def test_keeps_house_number_and_street_words(self):
        from scraper.nc_gis_lookup import street_only
        # the number is the anchor; only city/state/zip/unit tails go
        assert street_only("17619 State Highway 209, Hot Springs, NC 28743") \
            == "17619 State Highway 209"

    def test_empty(self):
        from scraper.nc_gis_lookup import street_only
        assert street_only(None) == ""
        assert street_only("   ") == ""


class TestGapVariant:
    """OneMap pads its situs fields ("2820   LAUREL PARK  HWY")."""

    def test_joins_tokens_with_wildcards(self):
        from scraper.nc_gis_lookup import _gap_variant
        assert _gap_variant("LAUREL PARK HWY") == "LAUREL%PARK%HWY"
        assert _gap_variant("944 JONES CREEK RD") == "944%JONES%CREEK%RD"

    def test_single_token_has_no_gap_form(self):
        from scraper.nc_gis_lookup import _gap_variant
        assert _gap_variant("SYLVA") is None
        assert _gap_variant("") is None


# ---------------------------------------------------------------------------
# owner-group resolver (multi-parcel tracts)
# ---------------------------------------------------------------------------

class TestSearchOwnerParcels:
    def test_group_sum_matches_listing(self, monkeypatch):
        import scraper.nc_gis_lookup as N
        spy = _QuerySpy([*GUFFEY, *NEIGHBOURS])
        monkeypatch.setattr(N, "_nc1map_query", spy)
        out = N.search_owner_parcels_in_nc1map(
            "0 Leon Barron Rd", "Jackson", target_acres=26.02)
        assert out is not None
        assert [p["parno"] for p in out["parcels"]] == \
            ["7528-56-9321", "7528-56-9996"]
        assert out["acres"] == pytest.approx(26.02)
        # the largest parcel is primary: it centers the viewer
        assert out["parno"] == "7528-56-9321"
        assert out["owner_name"] == "GUFFEY, FREDERICK RAD ETAL"

    def test_placeholder_token_never_reaches_the_query(self, monkeypatch):
        import scraper.nc_gis_lookup as N
        spy = _QuerySpy(GUFFEY)
        monkeypatch.setattr(N, "_nc1map_query", spy)
        N.search_owner_parcels_in_nc1map(
            "0 Leon Barron Rd, Sylva, NC 28779", "Jackson", target_acres=26.02)
        assert spy.calls, "no OneMap query was issued"
        for where in spy.calls:
            assert "28779" not in where
            assert "SYLVA" not in where.upper()
            assert "'0 LEON" not in where.upper()
            assert "LEON" in where.upper()

    def test_no_group_matches_target(self, monkeypatch):
        import scraper.nc_gis_lookup as N
        monkeypatch.setattr(N, "_nc1map_query", _QuerySpy([*GUFFEY, *NEIGHBOURS]))
        assert N.search_owner_parcels_in_nc1map(
            "0 Leon Barron Rd", "Jackson", target_acres=5.5) is not None  # ELDERS single
        assert N.search_owner_parcels_in_nc1map(
            "0 Leon Barron Rd", "Jackson", target_acres=99.0) is None

    def test_requires_target_acres(self, monkeypatch):
        import scraper.nc_gis_lookup as N
        monkeypatch.setattr(N, "_nc1map_query", _QuerySpy(GUFFEY))
        assert N.search_owner_parcels_in_nc1map(
            "0 Leon Barron Rd", "Jackson", target_acres=None) is None
        assert N.search_owner_parcels_in_nc1map(
            "0 Leon Barron Rd", "Jackson", target_acres=0) is None

    def test_empty_street_returns_none(self, monkeypatch):
        import scraper.nc_gis_lookup as N
        monkeypatch.setattr(N, "_nc1map_query", _QuerySpy([]))
        assert N.search_owner_parcels_in_nc1map(
            "0 Leon Barron Rd", "Jackson", target_acres=26.02) is None

    def test_unknown_county_returns_none(self, monkeypatch):
        import scraper.nc_gis_lookup as N
        spy = _QuerySpy(GUFFEY)
        monkeypatch.setattr(N, "_nc1map_query", spy)
        assert N.search_owner_parcels_in_nc1map(
            "0 Leon Barron Rd", "Nowhere", target_acres=26.02) is None
        assert spy.calls == []


# ---------------------------------------------------------------------------
# stored multi-PIN plumbing
# ---------------------------------------------------------------------------

class TestViewerUrlsCarryEveryPin:
    def test_viewer_map_url_prefers_parcel_numbers(self):
        from scraper.server import _viewer_map_url
        url = _viewer_map_url({
            "state": "NC", "county": "Jackson", "latitude": 35.28,
            "longitude": -83.27, "parcel_number": "7528-56-9321",
            "parcel_numbers": "7528-56-9321,7528-56-9996",
        })
        assert url.startswith("/static/gis_viewer.html?")
        assert "7528-56-9321%2C7528-56-9996" in url

    def test_single_pin_rows_unchanged(self):
        from scraper.server import _viewer_map_url
        url = _viewer_map_url({
            "state": "NC", "county": "Jackson", "latitude": 35.28,
            "longitude": -83.27, "parcel_number": "7528-56-9321",
        })
        assert "parcel=7528-56-9321" in url


class TestViewerMarkup:
    """static/gis_viewer.html must highlight the union of several PINs."""

    @pytest.fixture()
    def html(self):
        from pathlib import Path
        path = (Path(__file__).resolve().parent.parent
                / "static" / "gis_viewer.html")
        return path.read_text(encoding="utf-8")

    def test_splits_the_parcel_param_into_pins(self, html):
        assert "pinVariants" in html
        assert "split(/[,\\s;]+/)" in html

    def test_draws_every_matched_polygon(self, html):
        assert "view.graphics.addMany(gs)" in html
        assert "view.goTo(gs.length > 1 ? gs : gs[0]" in html

    def test_reports_the_group_total(self, html):
        assert "Parcels (\u2248" in html or "Parcels (" in html


class TestBackfillLinksKeepsPins:
    def test_rebuild_does_not_drop_the_second_pin(self, tmp_path, monkeypatch):
        import scraper.backfill_links as B
        import scraper.nc_gis_lookup as N
        db_path = tmp_path / "links.db"
        conn = _ensure_db(db_path)
        insert_property(
            conn=conn, source="zillow", source_listing_id="2056598775",
            url="https://www.zillow.com/homedetails/x/2056598775_zpid/",
            address="0 Leon Barron Rd, Sylva, NC 28779", city="Sylva",
            county="Jackson", state="NC", zip_code="28779",
            latitude=35.287029, longitude=-83.270756, price_cents=235000,
            acres=26.02, parcel_number="7528-56-9321")
        conn.execute("UPDATE properties SET parcel_numbers = ?, "
                     "status = 'archived'",
                     ("7528-56-9321,7528-56-9996",))
        conn.commit()
        conn.close()

        monkeypatch.setattr(B.config, "db_path", db_path)

        def _forbidden(*a, **k):
            raise AssertionError("backfill_links must not query OneMap "
                                 "for a row that already has coordinates")

        monkeypatch.setattr(N, "_lookup_parcel", _forbidden)
        monkeypatch.setattr(N, "search_address_in_nc1map", _forbidden)
        monkeypatch.setattr(N, "search_owner_parcels_in_nc1map", _forbidden)

        B.backfill_links(source="zillow")

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT parcel_number, parcel_numbers, gis_url "
                           "FROM properties").fetchone()
        conn.close()
        pins, stored, gis = row
        assert pins == "7528-56-9321"          # primary stays single-valued
        assert stored == "7528-56-9321,7528-56-9996"
        assert "7528-56-9321%2C7528-56-9996" in (gis or "")


class TestDetailPageShowsPins:
    """The detail page must show every PIN and map all of them."""

    @pytest.fixture()
    def app(self, tmp_path, monkeypatch):
        from scraper.server import app
        from scraper.config import config as cfg
        cfg.db_path = tmp_path / "test.db"
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        token = base64.b64encode(b"winmutt:1234asdf").decode()
        _orig = app.test_client

        def _authed(*a, **k):
            client = _orig(*a, **k)
            client.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
            return client

        monkeypatch.setattr(app, "test_client", _authed)
        return app

    @pytest.fixture()
    def prop_id(self, tmp_path):
        conn = _ensure_db(tmp_path / "test.db")
        _status, row = insert_property(
            conn=conn, source="zillow", source_listing_id="2056598775",
            url="https://www.zillow.com/homedetails/x/2056598775_zpid/",
            address="0 Leon Barron Rd, Sylva, NC 28779", city="Sylva",
            county="Jackson", state="NC", zip_code="28779",
            latitude=35.287029, longitude=-83.270756, price_cents=23500000,
            acres=26.02, parcel_number="7528-56-9321")
        pid = row["id"]
        conn.execute("UPDATE properties SET parcel_numbers = ? WHERE id = ?",
                     ("7528-56-9321,7528-56-9996", pid))
        conn.commit()
        conn.close()
        return pid

    def test_lists_every_pin(self, app, prop_id):
        with app.test_client() as client:
            html = client.get(f"/property/{prop_id}").get_data(as_text=True)
        assert "Parcel Numbers (2)" in html
        assert "7528-56-9321, 7528-56-9996" in html
        assert "Parcel Number:" not in html   # singular form is replaced

    def test_embedded_map_carries_both_pins(self, app, prop_id):
        with app.test_client() as client:
            html = client.get(f"/property/{prop_id}").get_data(as_text=True)
        assert "7528-56-9321%2C7528-56-9996" in html

    def test_single_pin_row_renders_singular(self, app, tmp_path):
        conn = _ensure_db(tmp_path / "test.db")
        _status, row = insert_property(
            conn=conn, source="zillow", source_listing_id="1", url=None,
            address="150 Piney Ridge Drive", city="Candler", county="Buncombe",
            state="NC", zip_code="28715", latitude=35.9, longitude=-82.7,
            price_cents=100000, acres=2.0, parcel_number="9651503971")
        pid = row["id"]
        conn.commit()
        conn.close()
        with app.test_client() as client:
            html = client.get(f"/property/{pid}").get_data(as_text=True)
        assert "<strong>Parcel Number:</strong> 9651503971" in html
        assert "Parcel Numbers" not in html
