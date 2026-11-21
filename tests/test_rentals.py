"""Rentals vertical tests: RentCast screen, scope, scraper, dashboard.

The product: GA tax sales within 100 mi of downtown Atlanta in towns where
RentCast says a small house rents near $1,000/mo. What must hold:

  * the zip screen judges rent band / listing counts / sale cheapness;
  * scope = screened counties (∩ radius), with a radius-only fallback and
    an EMPTY refusal when neither data file exists (never "all GA");
  * the scraper writes tax-only rows under its own source, keeps small
    town lots (the mountains MIN_ACRES floor must not apply), and rows
    carry the area market rent;
  * the dashboard serves rentals as its own board and keeps those rows
    OFF the mountains board.
"""
import base64
import json

import pytest

from scraper.config import config, VERTICALS, RENTAL_SOURCES
from scraper import rent_scope
from scraper.rentcast import judge, summarize_market


def _markets_file(tmp_path, entries):
    p = tmp_path / "rent_markets.json"
    p.write_text(json.dumps({"generated": "2026-11-21", "zips": entries}))
    return str(p)


def _atl_file(tmp_path, counties):
    p = tmp_path / "county_radius_atlanta.json"
    p.write_text(json.dumps({"counties": counties}))
    return str(p)


class TestRentCastScreen:
    """summarize_market + judge — the rent-band logic, no network."""

    def _payload(self, rent3=1050, listings3=12, median_price=140000):
        return {
            "zipCode": "30517",
            "rentalData": {
                "medianRent": 1100, "averageRent": 1150, "totalListings": 30,
                "dataByBedrooms": [
                    {"bedrooms": 3, "medianRent": rent3,
                     "totalListings": listings3},
                    {"bedrooms": 2, "medianRent": 950, "totalListings": 9},
                ],
            },
            "saleData": {"medianPrice": median_price, "totalListings": 40},
        }

    def test_summary_picks_small_home_rent(self):
        s = summarize_market(self._payload())
        assert s["rent_small_home"] == 1050
        assert s["rent_basis"] == "3bd"
        assert s["median_price"] == 140000

    def test_summary_falls_back_to_2bd(self):
        payload = self._payload()
        payload["rentalData"]["dataByBedrooms"] = [
            {"bedrooms": 4, "medianRent": 1500, "totalListings": 3},
            {"bedrooms": 2, "medianRent": 950, "totalListings": 6},
        ]
        s = summarize_market(payload)
        assert s["rent_small_home"] == 950
        assert s["rent_basis"] == "2bd"

    def test_pass_in_band_cheap_enough(self):
        res = judge(summarize_market(self._payload()))
        assert res["verdict"] == "pass"
        assert res["gross_yield_pct"] == round(1050 * 12 / 140000 * 100, 1)

    def test_fail_when_rent_far_off_band(self):
        res = judge(summarize_market(self._payload(rent3=1900)))
        assert res["verdict"] == "fail"

    def test_borderline_when_too_few_listings(self):
        res = judge(summarize_market(self._payload(listings3=1)),
                    min_listings=4)
        assert res["verdict"] == "borderline"

    def test_borderline_when_price_above_cap(self):
        res = judge(summarize_market(self._payload(median_price=300000)))
        assert res["verdict"] == "borderline"
        res = judge(summarize_market(self._payload(median_price=600000)))
        assert res["verdict"] == "fail"

    def test_no_data_when_no_rentals(self):
        payload = self._payload()
        payload.pop("rentalData")
        res = judge(summarize_market(payload))
        assert res["verdict"] == "no_data"


class TestRentScope:
    """County scope precedence + the county -> rent stamp."""

    def test_screened_counties_with_radius_intersection(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(rent_scope, "MARKETS_PATH", _markets_file(tmp_path, {
            "30517": {"verdict": "pass", "counties": ["jackson"],
                      "rent": 1000},
            "30547": {"verdict": "borderline", "counties": ["white"],
                      "rent": 900},
            "30102": {"verdict": "fail", "counties": ["cherokee"]},
            "31088": {"verdict": "pass", "counties": ["dooly"]},  # out of radius
        }))
        monkeypatch.setattr(rent_scope, "ATL_TABLE_PATH", _atl_file(tmp_path, {
            "GA/jackson": {"boundary_mi": 60, "measured": True},
            "GA/white": {"boundary_mi": 80, "measured": True},
            "GA/cherokee": {"boundary_mi": 35, "measured": True},
            "GA/dooly": {"boundary_mi": 150, "measured": True},
        }))
        monkeypatch.setattr(config, "RENT_RADIUS_MILES", 100.0)
        assert rent_scope.target_counties() == ["jackson", "white"]

    def test_unmeasured_counties_cannot_prove_in(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rent_scope, "MARKETS_PATH", str(tmp_path / "x"))
        monkeypatch.setattr(rent_scope, "ATL_TABLE_PATH", _atl_file(tmp_path, {
            "GA/houston": {"boundary_mi": 80, "measured": False},
            "GA/jackson": {"boundary_mi": 60, "measured": True},
        }))
        monkeypatch.setattr(config, "RENT_RADIUS_MILES", 100.0)
        assert rent_scope.target_counties() == ["jackson"]

    def test_screened_everything_out_means_no_scope(self, tmp_path,
                                                    monkeypatch):
        """A run of fails must NOT fall back to the radius — the screen
        answered, and its answer is "nothing here"."""
        monkeypatch.setattr(rent_scope, "MARKETS_PATH", _markets_file(tmp_path, {
            "30102": {"verdict": "fail", "counties": ["cherokee"]},
        }))
        monkeypatch.setattr(rent_scope, "ATL_TABLE_PATH", _atl_file(tmp_path, {
            "GA/cherokee": {"boundary_mi": 35, "measured": True},
        }))
        monkeypatch.setattr(config, "RENT_RADIUS_MILES", 100.0)
        assert rent_scope.target_counties() == []

    def test_no_files_means_no_scope(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rent_scope, "MARKETS_PATH", str(tmp_path / "a"))
        monkeypatch.setattr(rent_scope, "ATL_TABLE_PATH", str(tmp_path / "b"))
        assert rent_scope.target_counties() == []
        assert "NO SCOPE" in rent_scope.describe_scope()

    def test_county_rent_prefers_qualifying_zips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rent_scope, "MARKETS_PATH", _markets_file(tmp_path, {
            "30517": {"verdict": "pass", "counties": ["jackson"], "rent": 1000},
            "30518": {"verdict": "pass", "counties": ["jackson"], "rent": 1100},
            "30519": {"verdict": "fail", "counties": ["jackson"], "rent": 1800},
        }))
        assert rent_scope.county_rent("jackson") == 1050
        assert rent_scope.county_rent("nope") is None


class TestRentalsScraper:
    """GATaxRentalsScraper — tax-only policy, scope, small lots, source."""

    BUNDLED = (
        "PUBLIC NOTICE TAX SALE. COUNTY OF WHITE, GEORGIA. "
        "File #: 12 Map/Parcel Number: 045 012 Defendant DOE "
        "being known as 111 Holly Springs Road containing 1.5 acres. "
        "File #: 13 Map/Parcel Number: 045 013 Defendant ROE "
        "known as 222 Hollis Lane, 0.4 acres. "
        "Sale pursuant to tax sale law, first Tuesday in January 2027."
    )

    def _scraper(self):
        from scraper.ga_tax_rentals import GATaxRentalsScraper
        return GATaxRentalsScraper(solve_captcha=False)

    def test_source_is_a_rental_source(self):
        from scraper.ga_tax_rentals import GATaxRentalsScraper
        assert GATaxRentalsScraper.SOURCE_NAME in RENTAL_SOURCES

    def test_tax_sales_category_only(self):
        assert self._scraper()._get_categories() == "Tax Sales"

    def test_mortgage_signal_pinned_off(self):
        text = ("NOTICE OF SALE UNDER POWER — security deed, Georgia. "
                "Will sell lot 12 in block 4.")
        from scraper.ga_tax_rentals import GATaxRentalsScraper
        assert GATaxRentalsScraper._is_mortgage_foreclosure(text) is False

    def test_rows_written_under_rentals_source(self):
        props = self._scraper()._parse_parcels(
            self.BUNDLED, "white", "2027-01-05", "http://x/ID=1",
            kind="tax_foreclosure", source="ga_tax_rentals")
        assert props and all(p["source"] == "ga_tax_rentals" for p in props)

    def test_mountain_county_rows_keep_their_source(self):
        """ga_publicnotice defaults unchanged by the shared parser."""
        from scraper.ga_publicnotice import GAPublicNoticeScraper
        props = GAPublicNoticeScraper()._parse_parcels(
            self.BUNDLED, "white", "2027-01-05", "http://x/ID=1")
        assert props and all(p["source"] == "ga_publicnotice" for p in props)

    def test_small_lots_survive_the_filter(self, monkeypatch):
        scraper = self._scraper()
        monkeypatch.setattr(scraper, "_get_target_counties",
                            lambda: {"white"})
        props = [
            {"county": "White", "acres": 0.4},   # town lot — the product
            {"county": "White", "acres": None},  # unknown acres
            {"county": "Fulton", "acres": 2.0},  # outside scope
        ]
        kept = scraper._apply_county_acreage_filter(props)
        assert [p["county"] for p in kept] == ["White", "White"]

    def test_scope_comes_from_rent_scope(self, monkeypatch):
        from scraper import ga_tax_rentals
        monkeypatch.setattr(ga_tax_rentals.rent_scope, "target_counties",
                            lambda: ["jackson", "white"])
        assert self._scraper()._get_target_counties() == {"jackson", "white"}


class TestInsertPlumbing:
    def test_market_rent_inserted_and_refreshed(self, tmp_path):
        from scraper.db import _ensure_db, insert_property
        conn = _ensure_db(tmp_path / "rent.db")
        kw = dict(source="ga_tax_rentals", source_listing_id="r1", url=None,
                  address="111 Main St", city=None, county="White", state="GA",
                  zip_code=None, latitude=None, longitude=None,
                  price_cents=50000, property_type="tax_foreclosure")
        _, row = insert_property(conn, market_rent_monthly=1050, **kw)
        assert row["market_rent_monthly"] == 1050
        # A re-sighting without a value never clobbers it…
        _, row = insert_property(conn, market_rent_monthly=None, **kw)
        assert row["market_rent_monthly"] == 1050
        # …and a refreshed screen value wins.
        _, row = insert_property(conn, market_rent_monthly=990, **kw)
        assert row["market_rent_monthly"] == 990
        conn.close()

    def test_acreage_reap_exempts_rentals(self, tmp_path):
        from scraper.db import _ensure_db, insert_property, archive_below_acres
        conn = _ensure_db(tmp_path / "rent.db")
        base = dict(url=None, address=None, city=None, county="White",
                    state="GA", zip_code=None, latitude=None, longitude=None,
                    price_cents=10000, property_type="tax_foreclosure")
        insert_property(conn, source="ga_tax_rentals",
                        source_listing_id="keep-me", acres=0.4, **base)
        insert_property(conn, source="other", source_listing_id="reap-me",
                        acres=0.4, **base)
        archived = archive_below_acres(conn, 1.1,
                                       include_sources=["ga_tax_rentals",
                                                        "other"],
                                       exclude_sources=RENTAL_SOURCES)
        assert archived == 1
        status = dict(conn.execute(
            "SELECT source_listing_id, status FROM properties").fetchall())
        assert status["keep-me"] == "active"
        assert status["reap-me"] == "archived"
        conn.close()


class TestRentalsDashboard:
    """The '/' route: vertical boards, counts, and mountains isolation."""

    @pytest.fixture()
    def app(self, tmp_path, monkeypatch):
        from scraper.server import app
        from scraper.config import config as cfg
        cfg.db_path = tmp_path / "test.db"
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        token = base64.b64encode(b"winmutt:1234asdf").decode()
        orig = app.test_client

        def authed(*a, **kw):
            client = orig(*a, **kw)
            client.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
            return client

        monkeypatch.setattr(app, "test_client", authed)
        return app

    @staticmethod
    def _seed():
        from scraper.db import _ensure_db, insert_property
        conn = _ensure_db()
        base = dict(url=None, city=None, zip_code=None, latitude=None,
                    longitude=None, price_cents=100000)
        insert_property(conn, source="ga_publicnotice",
                        source_listing_id="mtg1", address="9 Mountain Rd",
                        county="White", state="GA", acres=5.0,
                        property_type="tax_foreclosure", **base)
        insert_property(conn, source="nc_publicnotice",
                        source_listing_id="nc1", address="8 Ridge Rd",
                        county="Macon", state="NC", acres=5.0,
                        property_type="tax_foreclosure", **base)
        insert_property(conn, source="ga_tax_rentals",
                        source_listing_id="rent1", address="111 Main St",
                        county="Jasper", state="GA",
                        property_type="tax_foreclosure", acres=0.4,
                        market_rent_monthly=1050, **base)
        conn.close()

    def test_rentals_row_not_on_mountains_board(self, app):
        self._seed()
        with app.test_client() as client:
            html = client.get("/?st=GA").get_data(as_text=True)
        assert "9 Mountain Rd" in html
        assert "111 Main St" not in html
        # GA's state-tab count excludes the rentals row (1, not 2).
        assert "GA <span class=\"tab-count\">1</span>" in html
        # Vertical tab carries its own count (1 rentals row, 2 mountains).
        assert 'href="/?vt=rentals"' in html

    def test_rentals_board_isolates_its_rows(self, app):
        self._seed()
        with app.test_client() as client:
            html = client.get("/?vt=rentals").get_data(as_text=True)
        assert "111 Main St" in html
        assert "9 Mountain Rd" not in html
        assert "8 Ridge Rd" not in html
        # GA is the only state on the rentals board.
        assert "1050" in html or "$1,050" in html or "≈$" in html

    def test_stub_vertical_renders_stub(self, app):
        self._seed()
        with app.test_client() as client:
            html = client.get("/?vt=beach").get_data(as_text=True)
        assert "coming soon" in html

    def test_unknown_vertical_falls_back_to_default(self, app):
        self._seed()
        with app.test_client() as client:
            html = client.get("/?vt=bogus&st=GA").get_data(as_text=True)
        assert "9 Mountain Rd" in html
