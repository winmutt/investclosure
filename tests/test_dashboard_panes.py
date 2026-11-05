"""Dashboard board/pane tests: per-state boards, per-tab filters and sorting.

Guards the bug the shared dashboard query used to have: ONE query with one row
window fed every state and tab, so Tennessee's ~1,700 're' rows starved NC's
board down to a handful of tiles even though NC had 975 active properties.
"""
import base64
import re

import pytest

from scraper.db import insert_property
from scraper.server import (
    PROPERTY_CATEGORIES,
    _category_clause,
    _pane_filters,
    property_category,
)
from werkzeug.datastructures import MultiDict


def _args(**kw):
    return MultiDict([(k, str(v)) for k, v in kw.items() if v is not None])


class TestPaneQueries:
    """The (state x category) WHERE clause and its filter parsing."""

    def test_category_clause_matches_python_classifier(self, tmp_path):
        """SQL buckets must agree with property_category() row by row."""
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "cat.db")
        rows = [
            dict(source="zillow", property_type="lot", notes=None,
                 description=None),                                  # re
            dict(source="zillow", property_type="mortgage_foreclosure",
                 notes=None, description=None),                      # re (source wins)
            dict(source="nc_publicnotice", property_type="mortgage_foreclosure",
                 notes=None, description=None),                      # mtg
            dict(source="nc_publicnotice", property_type=None,
                 notes="sold under deed of trust", description=None),  # mtg
            dict(source="nc_publicnotice", property_type="bank_owned",
                 notes=None, description=None),                      # mtg
            dict(source="newspaper_notices", property_type=None, notes=None,
                 description="Notice of sale under deed of trust"),    # mtg
            dict(source="nc_publicnotice", property_type="tax_foreclosure",
                 notes=None, description=None),                      # tax
            dict(source="haywood_county", property_type=None, notes=None,
                 description=None),                                  # tax
            dict(source="test", property_type="", notes="", description=""),  # tax
        ]
        for i, r in enumerate(rows):
            insert_property(conn=conn, source=r["source"], source_listing_id=f"R{i}",
                            url=None, address=f"{i} Road", city=None, county="Ashe",
                            state="NC", zip_code=None, latitude=None, longitude=None,
                            price_cents=1000, acres=10.0,
                            property_type=r["property_type"],
                            description=r["description"])
            if r["notes"]:
                # notes is a dashboard-editable column, not an insert field
                conn.execute("UPDATE properties SET notes = ?"
                             " WHERE source_listing_id = ?", (r["notes"], f"R{i}"))
        conn.commit()
        for i, r in enumerate(rows):
            want = property_category(r)
            for cat in PROPERTY_CATEGORIES:
                clause, _ = _category_clause(cat)
                hit = conn.execute(
                    f"SELECT 1 FROM properties WHERE source_listing_id = ?"
                    f" AND ({clause})", (f"R{i}",)).fetchone() is not None
                assert hit == (cat == want), (i, cat, want, hit)

    def test_pane_filters_read_namespaced_params(self):
        f = _pane_filters(_args(county_mtg="Macon", min_acres_mtg="10",
                                max_acres_mtg="50", sort_mtg="acres_desc"), "mtg")
        assert f == {"county": "Macon", "county_state": "", "min_acres": 10.0,
                     "max_acres": 50.0, "sort": "acres_desc"}

    def test_pane_filters_ignore_other_tabs_values(self):
        f = _pane_filters(_args(county_tax="Ashe", sort_re="acres_asc"), "mtg")
        assert f["county"] == ""
        assert f["sort"] == "sale"

    def test_pane_filters_fall_back_to_shared_bar_names(self):
        """Links written before the per-tab controls still filter every tab."""
        f = _pane_filters(_args(county="NC|Macon", min_acres="5", sort="acres_desc"), "re")
        assert f["county"] == "Macon"
        assert f["county_state"] == "NC"
        assert f["min_acres"] == 5.0
        assert f["sort"] == "acres_desc"

    def test_pane_filters_swap_reversed_bounds_and_drop_junk(self):
        f = _pane_filters(_args(min_acres_re="500", max_acres_re="10",
                                sort_re="DROP TABLE"), "re")
        assert (f["min_acres"], f["max_acres"]) == (10.0, 500.0)
        assert f["sort"] == "sale"
        assert _pane_filters(_args(min_acres_re="abc"), "re")["min_acres"] is None


class TestDashboardBoards:
    """The '/' route: one state board, three independently filtered tabs."""

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
    def _row(conn, lid, acres=10.0, county="Ashe", state="NC", source="test", **kw):
        base = dict(source=source, source_listing_id=lid, url=None,
                    address=f"{lid} Road", city=None, county=county, state=state,
                    zip_code=None, latitude=None, longitude=None,
                    price_cents=10000, acres=acres, property_type=None)
        base.update(kw)
        return insert_property(conn=conn, **base)[1]["id"]

    def test_boards_show_every_state_regardless_of_row_volume(self, app, tmp_path):
        """TN's big 're' tab must not crowd NC off the dashboard."""
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        for i in range(30):
            self._row(conn, f"TN{i}", state="TN", source="zillow", county="Knox")
        self._row(conn, "NC1", state="NC", county="Macon")
        self._row(conn, "NC2", state="NC", county="Macon", source="zillow")
        conn.close()

        with app.test_client() as client:
            nc = client.get("/?st=NC").get_data(as_text=True)
            tn = client.get("/?st=TN").get_data(as_text=True)
        assert "NC1 Road" in nc and "NC2 Road" in nc
        assert "TN0 Road" not in nc
        assert "TN0 Road" in tn and "NC1 Road" not in tn

    def test_each_tab_gets_its_own_row_budget(self, app, tmp_path):
        """The per-pane limit applies per tab, not across all tabs."""
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        for i in range(12):
            self._row(conn, f"RE{i}", source="zillow", county="Macon")
        self._row(conn, "TAX1", county="Macon")
        self._row(conn, "MTG1", county="Macon", property_type="mortgage_foreclosure")
        conn.close()

        with app.test_client() as client:
            html = client.get("/?st=NC&cat=tax").get_data(as_text=True)
        # All three tabs are rendered on one page: one tax, one mtg, all re.
        assert "TAX1 Road" in html and "MTG1 Road" in html and "RE11 Road" in html

    def test_tab_counts_follow_that_tabs_filters(self, app, tmp_path):
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        for county, acres in (("Macon", 8.0), ("Macon", 40.0), ("Ashe", 9.0)):
            self._row(conn, f"MTG-{county}", county=county, acres=acres,
                      property_type="mortgage_foreclosure")
        self._row(conn, "RE-OTHER", county="Macon", source="zillow", acres=9.0)
        conn.close()

        with app.test_client() as client:
            html = client.get("/?st=NC&cat=mtg&min_acres_mtg=10").get_data(as_text=True)
        # Mtg narrows to the one 40-acre Macon row; Re keeps its own count.
        tabs = dict(re.findall(
            r'data-tab="(tax|mtg|re)"[^>]*>\s*(?:Tax|Mtg|Re)\s*'
            r'<span class="tab-count">(\d+)</span>', html))
        assert tabs == {"tax": "0", "mtg": "1", "re": "1"}
        assert 'data-tab="mtg" aria-selected="true"' in html
        assert "MTG-Macon Road" in html
        assert "MTG-Ashe Road" not in html
        assert "RE-OTHER Road" in html      # other tabs are unaffected

    def test_county_options_are_scoped_to_the_tab(self, app, tmp_path):
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        self._row(conn, "TAX-ASHE", county="Ashe")
        self._row(conn, "RE-BUNCOMBE", county="Buncombe", source="zillow")
        conn.close()

        with app.test_client() as client:
            html = client.get("/?st=NC").get_data(as_text=True)
        # NB: match the selects, not the hidden inputs the other panes carry.
        tax_select = html.split('name="county_tax" class="filter-select"')[1].split("</select>")[0]
        re_select = html.split('name="county_re" class="filter-select"')[1].split("</select>")[0]
        assert "Ashe County" in tax_select and "Buncombe County" not in tax_select
        assert "Buncombe County" in re_select and "Ashe County" not in re_select

    def test_opens_first_tab_with_rows_and_honours_cat(self, app, tmp_path):
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        self._row(conn, "RE1", source="zillow", county="Macon")
        conn.close()

        with app.test_client() as client:
            default = client.get("/?st=NC").get_data(as_text=True)
        # No tax rows: the Re pane must be the visible one, not empty Tax.
        assert '<div class="tab-pane" data-tab-pane="tax" data-tab-pane-group="cat" hidden>' in default
        assert '<div class="tab-pane" data-tab-pane="re" data-tab-pane-group="cat">' in default

    def test_legacy_state_county_does_not_leak_into_another_board(self, app, tmp_path):
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        self._row(conn, "NCPOLK", state="NC", county="Polk")
        self._row(conn, "TNPOLK", state="TN", county="Polk")
        self._row(conn, "NCOTHER", state="NC", county="Ashe")
        conn.close()

        with app.test_client() as client:
            html = client.get("/?county=TN%7CPolk&st=NC").get_data(as_text=True)
        # The filter names Tennessee's board; NC's board stays unfiltered.
        assert "NCPOLK Road" in html and "NCOTHER Road" in html

    def test_state_tabs_link_to_each_board_and_keep_sort(self, app, tmp_path):
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        self._row(conn, "NC1", state="NC", county="Macon")
        self._row(conn, "TN1", state="TN", county="Knox")
        conn.close()

        with app.test_client() as client:
            html = client.get("/?st=NC&sort_re=acres_desc").get_data(as_text=True)
        assert 'href="/?st=TN&amp;sort_tax=sale&amp;sort_mtg=sale&amp;sort_re=acres_desc"' in html

    def test_pane_filter_form_posts_only_its_own_tab(self, app, tmp_path):
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        self._row(conn, "NC1", state="NC", county="Macon")
        conn.close()

        with app.test_client() as client:
            html = client.get("/?st=NC&cat=tax").get_data(as_text=True)
        bar = html.split('class="search-bar filter-bar filter-bar-pane"')[1]
        tax_form = bar.split("</form>")[0]
        assert 'name="st" value="NC"' in tax_form
        assert 'name="cat" value="tax"' in tax_form
        assert 'name="county_tax"' in tax_form
        # Other tabs' controls are carried as hidden fields so switching tabs
        # does not silently drop their filters.
        assert 'name="min_acres_mtg"' in tax_form and 'name="sort_re"' in tax_form

    def test_unknown_state_falls_back_to_first_board(self, app, tmp_path):
        from scraper.db import _ensure_db
        conn = _ensure_db(tmp_path / "test.db")
        self._row(conn, "NC1", state="NC", county="Macon")
        conn.close()

        with app.test_client() as client:
            resp = client.get("/?st=ZZ")
        assert resp.status_code == 200
        assert "NC1 Road" in resp.get_data(as_text=True)
