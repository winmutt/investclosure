"""Georgia qPublic acreage enrichment.

The old enricher deep-linked each county's parcel report with a GUESSED
``KeyValue`` spelling, and a wrong guess is invisible: Lumpkin's ``059-029``
became ``KeyValue=059++++-029`` and every load landed on app chrome reading
"No Global Search Results" — ``acres_source`` was NULL on all 673 GA rows, so
not one had ever been filled. It now searches through the county's own form
(``scraper/ga_gis_enrich.py``) and reads the record the app resolves.

These tests pin the parsing contract measured off the live sites (Barrow 635,
Lumpkin 991, Gilmer 672, Union 773), the queue rules that stopped an
unanswerable county from eating the whole batch, and the negative-cache
marker that stops re-paying a browser session for "this county publishes no
acreage".
"""
from contextlib import contextmanager

import pytest

from scraper import ga_gis_enrich as G
from scraper.db import _ensure_db, insert_property


# ---------------------------------------------------------------------------
# pure parsing
# ---------------------------------------------------------------------------

class TestNormalizeKey:
    def test_separators_and_case_are_ignored(self):
        assert G.normalize_key("059-029") == "059029"
        assert G.normalize_key("  au02b 006 ") == "AU02B006"
        assert G.normalize_key(None) == ""


class TestRecordFields:
    def test_only_two_cell_rows_are_fields(self):
        # Measured on Union 773 / Barrow 635 record reports: one-cell note
        # rows and the wide improvement/sale tables sit in the same <table>s.
        rows = [
            ["Parcel Number", "056C 074 A"],
            ["Account/Realkey", "6823"],
            ["(Note: Not to be used on legal documents)"],
            ["Zoning"],
            ["Tax District", "County (District 01)"],
            ["Type", "Description", "Calculation Method", "Acres"],
            ["RUR", "Small Parcel", "Rural", "1.22"],
        ]
        fields = G.record_fields(rows)
        assert fields["parcel number"] == "056C 074 A"
        assert fields["tax district"] == "County (District 01)"
        assert "zoning" not in fields          # label with no value
        assert "type" not in fields
        assert "rur" not in fields

    def test_first_value_wins(self):
        fields = G.record_fields([["Acres", "1.5"], ["Acres", "9.9"]])
        assert fields["acres"] == "1.5"


class TestRecordAcres:
    def test_positive_acres(self):
        assert G.record_acres({"acres": "0.7"}) == 0.7
        assert G.record_acres({"acres": "1,250.5"}) == 1250.5

    def test_zero_is_not_an_acreage(self):
        # Barrow platted lots and Rabun Sky Valley lots publish Acres 0.
        assert G.record_acres({"acres": "0"}) is None

    def test_alternate_label(self):
        assert G.record_acres({"acres": "0", "land acres": "3.2"}) == 3.2

    def test_absurd_value_rejected(self):
        assert G.record_acres({"acres": "100000"}) is None

    def test_publishes_no_acres(self):
        assert G.record_publishes_no_acres({"acres": "0"}) is True
        assert G.record_publishes_no_acres({"acres": "0.7"}) is False
        assert G.record_publishes_no_acres({}) is False


class TestAddressMatches:
    def test_abbreviated_street_type_matches(self):
        # Live: White/Gilmer records print "37 LAFAYETTE DR" for a listing
        # written "37 Lafayette Drive".
        assert G.address_matches("37 Lafayette Drive", "37 LAFAYETTE DR")

    def test_house_number_must_agree(self):
        assert G.address_matches("32 Bear Ridge Rd", "44 BEAR RIDGE RD") is False

    def test_road_name_alone_never_matches(self):
        # Neither side has a situs number: that is a road, not a parcel.
        assert G.address_matches("Wolf Pen Gap Rd", "WOLF PEN GAP RD") is False
        assert G.address_matches("0 Mule Branch Rd", "MULE BRANCH RD") is False

    def test_different_street_rejected(self):
        assert G.address_matches(
            "1696 Rocky Top Rd", "1696 ANGEL HILL RD") is False


class TestResultRows:
    # Gilmer renders "Parcel ID" and "Alternate ID" as two headers over one
    # cell, so the app's own KeyValue is the only dependable row identity.
    DUMP = [
        {"cells": ["Select", "Parcel ID", "Alternate ID", "Owner",
                   "Property Address", "Map"], "href": None},
        {"cells": ["056C 074 A", "6823", "KELLEY RONALD TERRY & PEGGY S",
                   "45 ANDERSON RD EXT", "Map"],
         "href": "https://q/Application.aspx?AppID=773&PageTypeID=4"
                 "&PageID=5643&Q=413&KeyValue=056C+++074+A"},
        {"cells": ["007 047 A07", "17364", "REED CRAIG & SHANNON TRUST",
                   "146 ANGEL HILL RD", "Map"],
         "href": "https://q/Application.aspx?KeyValue=007++++047+A07"},
    ]

    def test_only_rows_with_a_key_link_survive(self):
        assert len(G.result_rows(self.DUMP)) == 2

    def test_row_key_reads_the_apps_own_keyvalue(self):
        rows = G.result_rows(self.DUMP)
        assert G.row_key(rows[0]) == "056C074A"

    def test_parcel_matches_despite_our_separators(self):
        rows = G.result_rows(self.DUMP)
        assert G.pick_result_row(rows, parcel="056C-074-A") is rows[0]

    def test_unknown_parcel_matches_nothing(self):
        assert G.pick_result_row(G.result_rows(self.DUMP),
                                 parcel="ZZ99 111") is None

    def test_single_row_is_enough_for_an_address_search(self):
        one = G.result_rows(self.DUMP)[:1]
        assert G.pick_result_row(one, address="45 Anderson Rd Ext") is one[0]

    def test_ambiguous_address_list_is_refused(self):
        rows = G.result_rows(self.DUMP)
        assert G.pick_result_row(rows, address="99 Somewhere Rd") is None

    def test_address_picks_the_row_whose_cell_carries_the_number(self):
        rows = G.result_rows(self.DUMP)
        got = G.pick_result_row(rows, address="146 Angel Hill Rd, Blairsville")
        assert got is rows[1]


class TestDetailUrl:
    def test_session_nonce_is_dropped(self):
        assert G.clean_detail_url(
            "https://q/Application.aspx?AppID=1&Q=12345&KeyValue=X"
        ) == "https://q/Application.aspx?AppID=1&KeyValue=X"


class TestSearchPageCandidates:
    def test_decodes_entities_and_drops_the_map_landing(self):
        hrefs = [
            "/Application.aspx?AppID=635&amp;LayerID=11218&amp;PageTypeID=2&amp;PageID=4614",
            "/Application.aspx?AppID=635&PageTypeID=2&PageID=0",
            "Application.aspx?AppID=991&LayerID=20168&PageTypeID=2&PageID=8780",
            "/Application.aspx?AppID=635&PageTypeID=1&PageID=8779",
        ]
        assert G.search_page_candidates(hrefs) == [
            "https://qpublic.schneidercorp.com/Application.aspx"
            "?AppID=635&LayerID=11218&PageTypeID=2&PageID=4614",
            "https://qpublic.schneidercorp.com/Application.aspx"
            "?AppID=991&LayerID=20168&PageTypeID=2&PageID=8780",
        ]


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------

def _insert(conn, source, county, parcel=None, address=None):
    _kind, row = insert_property(
        conn, source=source,
        source_listing_id=f"{source}:{county}:{parcel or address}",
        url="http://x", address=address, city=None, county=county,
        state="GA", zip_code=None, latitude=None, longitude=None,
        price_cents=0, acres=None, parcel_number=parcel)
    return row["id"]


@pytest.fixture
def ga_db(tmp_path, monkeypatch):
    from scraper.config import config
    db_path = tmp_path / "ga.db"
    _ensure_db(db_path).close()
    monkeypatch.setattr(config, "db_path", db_path)
    return db_path


class _FakePage:
    """enrich_db only ever passes this to the patched browser functions."""

    def set_viewport_size(self, size):  # noqa: ARG002 - real page only
        return None


@pytest.fixture
def fake_browser(monkeypatch, tmp_path):
    """Patch the browser layer: discovery + record lookup are canned.

    ``save_apps`` is a no-op here — the real one writes
    ``scraper/ga_qpublic_apps.json`` (volume-mounted), and a canned
    ``https://q/<county>`` search URL must never reach that file.
    """
    calls = {"search": [], "discover": []}

    @contextmanager
    def _ctx():
        yield _FakePage()

    def _discover(page, county):
        calls["discover"].append(county)
        return {"found": True, "search_url": f"https://q/{county}"}

    monkeypatch.setattr(G, "camoufox_context", _ctx)
    monkeypatch.setattr(G, "discover_county", _discover)
    monkeypatch.setattr(G, "save_apps", lambda apps: None)
    monkeypatch.setattr(G, "load_apps", lambda: {})
    return calls


def _patch_records(monkeypatch, records):
    """``fetch_record`` returns records[key] keyed by (kind, value)."""
    def _fetch(page, search_url, kind, value, parcel=None, address=None):
        key = (kind, value)
        rec = records.get(key)
        if rec is None:
            return None
        return dict(rec)
    monkeypatch.setattr(G, "fetch_record", _fetch)


class TestEnrichDb:
    def test_acres_and_provenance_are_written(self, ga_db, fake_browser,
                                              monkeypatch):
        conn = _ensure_db(ga_db)
        pid = _insert(conn, "ga_publicnotice", "Lumpkin", parcel="059-029")
        conn.close()
        _patch_records(monkeypatch, {
            ("pin", "059-029"): {
                "fields": {"parcel number": "059 029", "acres": "0.7"},
                "acres": 0.7,
                "url": "https://q/record?KeyValue=059++++029",
            }})
        stats = G.enrich_db()
        assert (stats["processed"], stats["updated"]) == (1, 1)
        conn = _ensure_db(ga_db)
        row = conn.execute("SELECT acres, acres_source, gis_url FROM properties"
                           " WHERE id=?", (pid,)).fetchone()
        conn.close()
        assert row["acres"] == 0.7
        assert row["acres_source"] == "qpublic"
        assert row["gis_url"].endswith("KeyValue=059++++029")

    def test_no_acres_published_is_settled_once(self, ga_db, fake_browser,
                                                monkeypatch):
        conn = _ensure_db(ga_db)
        pid = _insert(conn, "ga_tax_rentals", "Barrow", parcel="AU02B 006")
        conn.close()
        _patch_records(monkeypatch, {
            ("pin", "AU02B 006"): {
                "fields": {"parcel number": "AU02B 006", "acres": "0"},
                "acres": None,
                "url": "https://q/record?KeyValue=AU02B++006",
            }})
        stats = G.enrich_db()
        assert stats["no_acres_published"] == 1
        conn = _ensure_db(ga_db)
        row = conn.execute("SELECT acres, acres_source FROM properties"
                           " WHERE id=?", (pid,)).fetchone()
        conn.close()
        assert row["acres"] is None
        assert row["acres_source"] == "qpublic_none"
        # ...and the second pass has nothing left to do.
        assert G.enrich_db()["processed"] == 0

    def test_record_without_an_acres_field_stays_open(self, ga_db, fake_browser,
                                                      monkeypatch):
        """A page that failed to render must not be cached as "no acreage"."""
        conn = _ensure_db(ga_db)
        pid = _insert(conn, "ga_publicnotice", "Gilmer",
                      address="37 Lafayette Drive, Ellijay, GA")
        conn.close()
        _patch_records(monkeypatch, {
            ("address", "37 Lafayette Drive"): {
                "fields": {"parcel number": "3051K 001"},
                "acres": None,
                "url": "https://q/record?KeyValue=3051K++001",
            }})
        stats = G.enrich_db()
        assert stats["unmatched"] == 1
        conn = _ensure_db(ga_db)
        row = conn.execute("SELECT acres, acres_source FROM properties"
                           " WHERE id=?", (pid,)).fetchone()
        conn.close()
        assert row["acres"] is None and row["acres_source"] is None

    def test_unanswerable_county_cannot_starve_the_batch(
            self, ga_db, fake_browser, monkeypatch):
        """94 rows of a county with no parcel app used to fill the LIMIT."""
        conn = _ensure_db(ga_db)
        for i in range(5):
            _insert(conn, "ga_tax_rentals", "Somecounty", parcel=f"P{i}")
        answerable = _insert(conn, "ga_tax_rentals", "Lumpkin",
                             parcel="059-029")
        conn.close()
        monkeypatch.setattr(G, "load_apps", lambda: {
            "somecounty": {"found": False}})
        _patch_records(monkeypatch, {
            ("pin", "059-029"): {"fields": {"acres": "5"}, "acres": 5.0,
                                 "url": "https://q/r"}})
        stats = G.enrich_db(limit=1)
        assert stats["skipped_no_app"] == 5
        assert stats["updated"] == 1
        conn = _ensure_db(ga_db)
        acres = conn.execute("SELECT acres FROM properties WHERE id=?",
                             (answerable,)).fetchone()["acres"]
        conn.close()
        assert acres == 5.0

    def test_small_rental_lot_survives_the_acreage_rule(
            self, ga_db, fake_browser, monkeypatch):
        conn = _ensure_db(ga_db)
        pid = _insert(conn, "ga_tax_rentals", "Bibb", parcel="BC67-0120")
        conn.close()
        _patch_records(monkeypatch, {
            ("pin", "BC67-0120"): {"fields": {"acres": "0.41"}, "acres": 0.41,
                                   "url": "https://q/r"}})
        G.enrich_db()
        conn = _ensure_db(ga_db)
        status = conn.execute("SELECT status FROM properties WHERE id=?",
                              (pid,)).fetchone()["status"]
        conn.close()
        assert status == "active"

    def test_mountains_row_below_min_acres_is_archived(
            self, ga_db, fake_browser, monkeypatch):
        conn = _ensure_db(ga_db)
        pid = _insert(conn, "ga_publicnotice", "Union", parcel="056C 074 A")
        conn.close()
        _patch_records(monkeypatch, {
            ("pin", "056C 074 A"): {"fields": {"acres": "1.22"},
                                    "acres": 0.5, "url": "https://q/r"}})
        G.enrich_db()
        conn = _ensure_db(ga_db)
        row = conn.execute("SELECT status, archive_reason FROM properties"
                           " WHERE id=?", (pid,)).fetchone()
        conn.close()
        assert row["status"] == "archived"
        assert "qpublic" in (row["archive_reason"] or "")

    def test_only_active_rows_are_processed(self, ga_db, fake_browser,
                                            monkeypatch):
        conn = _ensure_db(ga_db)
        pid = _insert(conn, "ga_publicnotice", "Fannin", parcel="0016 012C")
        conn.execute("UPDATE properties SET status='archived' WHERE id=?",
                     (pid,))
        conn.commit()
        conn.close()
        _patch_records(monkeypatch, {})
        assert G.enrich_db()["processed"] == 0


# ---------------------------------------------------------------------------
# discovery honesty — a county is blacklisted only when the COUNTY said so
# ---------------------------------------------------------------------------

class _QPage:
    """Stand-in for the pages ``discover_county`` drives.

    ``evaluate`` is dispatched by script identity, so a test says exactly what
    the county's pages link and which of them carries a search field.
    """

    def __init__(self, links=None, fields=None, broken=()):
        self.links = links if links is not None else []
        self.fields = fields or {}          # url -> {"id", "pane"}
        self.broken = set(broken)           # urls whose goto raises
        self.visited = []

    def set_viewport_size(self, size):  # noqa: ARG002
        return None

    def goto(self, url, **kwargs):
        if url in self.broken:
            raise RuntimeError("navigation timed out")
        self.visited.append(url)

    def wait_for_timeout(self, ms):
        return None

    def evaluate(self, script, arg=None):
        if script is G._LINKS_JS:
            return self.links
        if script is G._FIELD_JS:
            return self.fields.get(self.visited[-1])
        raise AssertionError("unexpected evaluate call")


class TestAppLandingUrl:
    def test_the_apps_own_capitalisation(self):
        # qPublic's slug is "DeKalbCountyGA"; "dekalb" answers an error page.
        assert G.app_landing_url("dekalb").endswith("App=DeKalbCountyGA")
        assert G.app_landing_url("McIntosh").endswith("App=McIntoshCountyGA")
        assert G.app_landing_url("Lumpkin").endswith("App=LumpkinCountyGA")


class TestDiscoverCounty:
    """Three outcomes, and only two of them are facts about a county."""

    SEARCH = "/Application.aspx?AppID=991&amp;LayerID=20168&amp;" \
             "PageTypeID=2&amp;PageID=8780"

    def _url(self):
        return G.search_page_candidates([self.SEARCH])[0]

    def test_landing_failure_is_not_an_answer(self):
        page = _QPage(broken=[G.app_landing_url("bibb")])
        assert G.discover_county(page, "Bibb") is None

    def test_a_landing_linking_nothing_is_not_an_answer(self):
        # A county with no qPublic app and a page that never rendered look
        # identical here, so neither is cached.
        assert G.discover_county(_QPage(links=[]), "Fannin") is None

    def test_every_candidate_failing_to_render_is_not_an_answer(self):
        url = self._url()
        page = _QPage(links=[self.SEARCH], broken=[url])
        assert G.discover_county(page, "Lumpkin") is None

    def test_the_page_with_a_search_field_wins(self):
        url = self._url()
        page = _QPage(links=[self.SEARCH],
                      fields={url: {"id": "ctlBodyPane_ctl02_ctl01_txtParcelID",
                                    "pane": "ctlBodyPane_ctl02_ctl01"}})
        meta = G.discover_county(page, "Lumpkin")
        assert meta["found"] is True
        assert (meta["app_id"], meta["layer_id"], meta["search_page_id"]) \
            == ("991", "20168", "8780")
        assert meta["search_url"] == url

    def test_a_county_with_rendered_pages_but_no_search_is_an_answer(self):
        page = _QPage(links=[self.SEARCH])       # renders, no field
        assert G.discover_county(page, "Lumpkin") == {
            "found": False, "reason": "no_search_page"}


@pytest.fixture
def strict_browser(monkeypatch):
    """Browser stubbed, registry writes recorded, discovery set per test."""
    calls = {"discover": [], "saved": []}

    @contextmanager
    def _ctx():
        yield _FakePage()

    monkeypatch.setattr(G, "camoufox_context", _ctx)
    monkeypatch.setattr(G, "load_apps", lambda: {})
    monkeypatch.setattr(G, "save_apps",
                        lambda apps: calls["saved"].append(dict(apps)))

    def _stub(result):
        def _discover(page, county):
            calls["discover"].append(county)
            return result
        monkeypatch.setattr(G, "discover_county", _discover)

    calls["stub"] = _stub
    return calls


class TestDiscoveryIsHonest:
    def test_could_not_answer_is_never_cached(self, ga_db, strict_browser):
        """A browser that had a bad minute must not blacklist a county for good."""
        conn = _ensure_db(ga_db)
        _insert(conn, "ga_tax_rentals", "Bibb", parcel="BC67-0120")
        conn.commit()
        conn.close()
        strict_browser["stub"](None)
        stats = G.enrich_db()
        assert stats["failed"] == 1
        assert strict_browser["saved"] == []
        # ...and the row is still queued for the next pass.
        assert G.enrich_db()["processed"] == 1

    def test_a_county_is_asked_once_per_pass(self, ga_db, strict_browser):
        conn = _ensure_db(ga_db)
        for i in range(3):
            _insert(conn, "ga_tax_rentals", "Bibb", parcel=f"BC67-01{i}")
        conn.commit()
        conn.close()
        strict_browser["stub"](None)
        stats = G.enrich_db()
        assert stats["processed"] == 3
        assert strict_browser["discover"] == ["bibb"]

    def test_no_search_page_is_cached_once(self, ga_db, strict_browser):
        conn = _ensure_db(ga_db)
        _insert(conn, "ga_tax_rentals", "Fannin", parcel="0016 012C")
        conn.commit()
        conn.close()
        strict_browser["stub"]({"found": False, "reason": "no_search_page"})
        stats = G.enrich_db()
        assert stats["skipped_no_app"] == 1
        assert strict_browser["saved"][-1]["fannin"]["found"] is False

