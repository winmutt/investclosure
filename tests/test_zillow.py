"""Tests for the ported Zillow scraper (pure parsing — no browser)."""
import json
from urllib.parse import parse_qs, urlparse

from scraper.zillow import (
    build_county_url, county_slug, county_display, lot_acres,
    bed_bath_counts, parse_county_page, extract_detail,
)


def _next_data(payload: dict) -> str:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'


def _listing(zpid, price, lot=None, unit="acres", state="NC",
             address="1 Empty Road, Sparta, NC 28675", home_type="LOT",
             lat=36.5, lon=-81.1, city="Sparta", zipcode="28675",
             bedrooms=None, bathrooms=None):
    home_info = {"price": price, "state": state, "homeType": home_type,
                 "streetAddress": address}
    if lot is not None:
        home_info["lotAreaValue"] = lot
        home_info["lotAreaUnit"] = unit
    if bedrooms is not None:
        home_info["bedrooms"] = bedrooms
    if bathrooms is not None:
        home_info["bathrooms"] = bathrooms
    return {
        "zpid": zpid,
        "address": address,
        "unformattedPrice": price,
        "detailUrl": f"https://www.zillow.com/homedetails/{zpid}_zpid/",
        "addressCity": city,
        "addressZipcode": zipcode,
        "hdpData": {"homeInfo": home_info},
        "latLong": {"latitude": lat, "longitude": lon},
        "imgSrc": f"https://images.zillowcdn.com/{zpid}.webp",
    }


def _page(items):
    return _next_data({"props": {"pageProps": {"searchPageState": {"cat1": {
        "searchResults": {"listResults": list(items)}}}}}})


def test_slugs():
    assert county_slug("Buncombe") == "buncombe"
    assert county_slug("van_buren") == "van-buren"
    assert county_slug("De Kalb") == "de-kalb"
    assert county_display("van_buren") == "Van Buren"
    assert build_county_url("van_buren", "TN") == \
        "https://www.zillow.com/homes/van-buren-county-tn/"
    # County/city name collisions must resolve to the COUNTY (madison-nc
    # serves the town of Madison, Rockingham Co., not Madison County).
    assert build_county_url("madison", "NC") == \
        "https://www.zillow.com/homes/madison-county-nc/"
    assert build_county_url("Buncombe", "nc") == \
        "https://www.zillow.com/homes/buncombe-county-nc/"


def test_lot_acres_units():
    assert lot_acres({"lotAreaValue": 5, "lotAreaUnit": "acres"}) == 5
    assert lot_acres({"lotAreaValue": 43560, "lotAreaUnit": "sqft"}) == 1.0
    assert lot_acres({}) is None
    assert lot_acres({"lotAreaValue": "junk", "lotAreaUnit": "acres"}) is None


def test_bed_bath_counts():
    assert bed_bath_counts({"bedrooms": 3, "bathrooms": 2}) == (3, 2.0)
    assert bed_bath_counts({"bedrooms": 4, "bathrooms": 2.5}) == (4, 2.5)
    assert bed_bath_counts({}) == (None, None)
    # Absent/nonsense/zero values must not render as "0 bd".
    assert bed_bath_counts({"bedrooms": 0, "bathrooms": 0}) == (None, None)
    assert bed_bath_counts({"bedrooms": "junk", "bathrooms": None}) == \
        (None, None)


def test_parse_bed_bath_present_and_absent():
    items = [
        _listing(1, 150000, lot=12.5, home_type="SINGLE_FAMILY",
                 bedrooms=3, bathrooms=2),
        _listing(2, 100000, lot=8.0),  # land: no bed/bath in payload
    ]
    props = parse_county_page(_page(items), "ashe", "NC")
    by_id = {p["source_listing_id"]: p for p in props}
    assert by_id["1"]["bedrooms"] == 3
    assert by_id["1"]["bathrooms"] == 2.0
    assert by_id["2"]["bedrooms"] is None
    assert by_id["2"]["bathrooms"] is None


def test_parse_county_page_basic():
    html = _page([_listing(463526404, 150000, lot=12.5)])
    props = parse_county_page(html, "alleghany", "NC")
    assert len(props) == 1
    p = props[0]
    assert p["source_listing_id"] == "463526404"
    assert p["county"] == "Alleghany"
    assert p["state"] == "NC"
    assert p["price"] == 150000.0
    assert p["acres"] == 12.5
    assert p["latitude"] == 36.5
    assert p["property_type"] == "lot"


def test_parse_filters_price_and_acres():
    items = [
        _listing(1, 500000, lot=10.0),        # over max price
        _listing(2, 100000, lot=0.5),         # below min acres
        _listing(3, 100000, lot=None),        # no acreage -> can't verify
        _listing(3, 120000, lot=8.0),         # dup zpid
        _listing(4, 90000, lot=87120, unit="sqft"),  # 2 acres via sqft
    ]
    props = parse_county_page(_page(items), "ashe", "NC",
                              max_price=400000, min_acres=1.1)
    ids = [p["source_listing_id"] for p in props]
    assert ids == ["4"]
    assert props[0]["acres"] == 2.0


def test_parse_requires_known_acreage():
    """Unknown lot size is not enough to qualify (2026-11-02 policy).

    A paginated county sweep returns thousands of listings with no acreage;
    the 5-acre rule can't be verified for them and the GIS enricher can't
    backfill that volume.
    """
    items = [
        _listing(1, 100000, lot=None),
        _listing(2, 100000, lot=5.0),
    ]
    props = parse_county_page(_page(items), "ashe", "NC", min_acres=5.0)
    assert [p["source_listing_id"] for p in props] == ["2"]


def test_parse_rejects_cross_state():
    items = [
        _listing(1, 100000, lot=5.0, state="KS",
                 address="905 2nd St, Perry, KS 66073"),
        _listing(2, 120000, lot=5.0, state="KY"),
    ]
    props = parse_county_page(_page(items), "perry", "KY")
    assert [p["source_listing_id"] for p in props] == ["2"]


def test_parse_bad_html():
    assert parse_county_page("", "ashe", "NC") == []
    assert parse_county_page("<html>no json</html>", "ashe", "NC") == []
    assert parse_county_page(_next_data({"props": {}}), "ashe", "NC") == []


def test_parse_rejects_far_from_county():
    # Macon County NC centroid vs a listing in Warren County (~5° away)
    centroids = {"NC|macon": [35.1437, -83.3977]}
    items = [
        _listing(1, 200000, lot=45.0, lat=36.389404, lon=-78.09051,
                 address="206 Shaver Rd, Macon, NC 27551"),
        _listing(2, 120000, lot=8.0, lat=35.15, lon=-83.40),
    ]
    props = parse_county_page(_page(items), "macon", "NC",
                              centroids=centroids)
    assert [p["source_listing_id"] for p in props] == ["2"]


def test_parse_applies_max_acres():
    items = [
        _listing(1, 239000, lot=107195.0, state="TN"),   # lot-size glitch
        _listing(2, 120000, lot=40.0, state="TN"),
    ]
    props = parse_county_page(_page(items), "grundy", "TN",
                                min_acres=5.0, max_acres=1000.0)
    assert [p["source_listing_id"] for p in props] == ["2"]


def test_build_paged_county_url():
    from scraper.zillow import build_paged_county_url

    def state_of(url):
        return json.loads(parse_qs(urlparse(url).query)["searchQueryState"][0])

    # Modern county page: filters live in searchQueryState, which this page
    # honours (the legacy /homes/for_sale/ path cannot express HOA).
    url = build_paged_county_url("macon", "NC")
    parsed = urlparse(url)
    assert parsed.path == "/macon-county-nc/"
    state = state_of(url)
    assert state["pagination"]["currentPage"] == 1
    assert "price" not in state["filterState"]          # 0 means no cap
    assert state["filterState"]["hoa"] == {"min": 0, "max": 0}
    assert state["filterState"]["includeHomesWithNoHoaData"] == {"value": True}

    # -county- slug, page segment and price cap all survive.
    url = build_paged_county_url("van_buren", "TN", page=3, max_price=250000)
    assert urlparse(url).path == "/van-buren-county-tn/3_p/"
    state = state_of(url)
    assert state["pagination"]["currentPage"] == 3
    assert state["filterState"]["price"] == {"min": 0, "max": 250000}

    # HOA filtering can be turned off (diagnostics / if Zillow changes it).
    url = build_paged_county_url("ashe", "NC", exclude_hoa=False)
    assert "hoa" not in state_of(url)["filterState"]
    assert "includeHomesWithNoHoaData" not in state_of(url)["filterState"]


def test_hoa_fee_reads_a_declared_monthly_fee():
    from scraper.zillow import hoa_fee
    assert hoa_fee({"monthlyHoaFee": 301}) == 301
    assert hoa_fee({"hoaFee": "125"}) == 125
    assert hoa_fee({"monthlyHoaFee": 0}) is None
    assert hoa_fee({"monthlyHoaFee": None}) is None
    assert hoa_fee({"monthlyHoaFee": True}) is None   # bool is not a fee
    assert hoa_fee({}) is None


def test_listings_with_a_declared_hoa_fee_are_dropped():
    """The URL filter is primary; a fee in the payload still never passes."""
    with_fee = _listing(2, 100000, lot=10, home_type="SINGLE_FAMILY")
    with_fee["hdpData"]["homeInfo"]["monthlyHoaFee"] = 275
    items = [_listing(1, 100000, lot=10), with_fee]
    props = parse_county_page(_page(items), "macon", "NC", min_acres=5.0)
    assert [p["source_listing_id"] for p in props] == ["1"]


def test_extract_list_results_distinguishes_wall_from_empty():
    from scraper.zillow import extract_list_results
    assert extract_list_results("") is None
    assert extract_list_results("<html>Access to this page has been denied"
                                "</html>") is None
    assert extract_list_results(_next_data({"props": {}})) is None
    assert extract_list_results(_page([])) == []
    assert len(extract_list_results(_page([_listing(1, 100000)]))) == 1


def _many(start, count, lot=10.0):
    return [_listing(start + i, 100000, lot=lot) for i in range(count)]


def _scraper(monkeypatch, fetch):
    from scraper import zillow as zl
    scraper = zl.ZillowScraper()
    scraper.max_price = 250000
    scraper.min_acres = 5.0
    scraper.centroids = {}          # fixtures aren't at Macon's coordinates
    scraper.geo = None
    scraper.DELAY_BETWEEN_SEARCH_PAGES = (0, 0)
    monkeypatch.setattr(scraper, "_fetch_page", fetch)
    return scraper


def test_county_walk_paginates_until_short_page(monkeypatch):
    """The county walk must follow the pager and union every page.

    Regression: zillow read only page 1 of 41 listings, so 491 Otter
    Mountain Rd (Macon NC, $235k/20.75ac) — which sits on page 12 — was
    never harvested.
    """
    pages = {
        1: _many(1000, 41),                        # full page -> keep going
        2: _many(2000, 41, lot=20.75),             # full page -> keep going
        3: _many(3000, 7),                         # short page -> last page
    }

    def fake_fetch(url, county, state, page_no, attempts=1):
        parsed = urlparse(url)
        state_json = json.loads(
            parse_qs(parsed.query)["searchQueryState"][0])
        assert state_json["filterState"]["price"] == {"min": 0, "max": 250000}
        assert state_json["filterState"]["hoa"] == {"min": 0, "max": 0}
        assert (f"/{page_no}_p/" in parsed.path) == (page_no > 1), url
        return _page(pages[page_no]) if page_no in pages else None

    scraper = _scraper(monkeypatch, fake_fetch)
    props, challenged, returned, n_pages = scraper._scrape_county("macon", "NC")
    assert challenged is False
    assert n_pages == 3
    assert returned == 89                      # 41 + 41 + 7 raw listings
    assert len(props) == 89
    assert "2000" in {p["source_listing_id"] for p in props}
    assert props[41]["acres"] == 20.75


def test_county_walk_short_first_page_does_not_fetch_page_two(monkeypatch):
    """A page with fewer than 41 listings is the whole county."""
    calls = []

    def fake_fetch(url, county, state, page_no, attempts=1):
        calls.append(page_no)
        return _page(_many(1000, 3))

    scraper = _scraper(monkeypatch, fake_fetch)
    props, challenged, returned, n_pages = scraper._scrape_county("ashe", "NC")
    assert calls == [1]
    assert (n_pages, returned, len(props)) == (1, 3, 3)


def test_county_walk_stops_on_bot_page(monkeypatch):
    """A walled later page keeps what was already harvested."""
    def fake_fetch(url, county, state, page_no, attempts=1):
        if page_no == 1:
            return _page(_many(1000, 41))
        return None

    scraper = _scraper(monkeypatch, fake_fetch)
    props, challenged, returned, n_pages = scraper._scrape_county("ashe", "NC")
    assert challenged is False            # page 1 was fine
    assert n_pages == 1
    assert returned == 41
    assert len(props) == 41


def test_county_walk_stops_on_repeated_page(monkeypatch):
    """If Zillow ignores the offset and replays page 1, stop (no loops)."""
    same = _page(_many(1000, 41))
    calls = []

    def fake_fetch(url, county, state, page_no, attempts=1):
        calls.append(page_no)
        return same

    scraper = _scraper(monkeypatch, fake_fetch)
    props, challenged, returned, n_pages = scraper._scrape_county("ashe", "NC")
    assert calls == [1, 2]
    assert n_pages == 2
    assert len(props) == 41               # duplicates not re-added
    assert returned == 82


def test_enforce_thresholds_archives_strays(monkeypatch, tmp_path):
    """Post-enrichment reap: price cap + acreage bounds on active rows."""
    import sqlite3
    from scraper import zillow as zl

    db = tmp_path / "enforce.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, status TEXT,
        price_cents INTEGER, acres REAL, archived_at TEXT,
        archived_by TEXT, archive_reason TEXT)""")
    rows = [
        ("zillow", "active", 26_000_000, 10.0),   # over $250k -> archive
        ("zillow", "active", 15_000_000, 3.0),    # under min acres -> archive
        ("zillow", "active", 15_000_000, 2000.0),  # over max acres -> archive
        ("zillow", "active", 15_000_000, 10.0),   # keeps
        ("zillow", "active", 15_000_000, None),   # NULL acres -> archive
        ("zillow", "archived", 99_000_000, 1.0),  # already archived
        ("kania_law", "active", 99_000_000, 1.0),  # other source untouched
    ]
    conn.executemany("INSERT INTO properties (source, status, price_cents,"
                     " acres) VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(zl.config, "db_path", str(db))
    result = zl.enforce_thresholds()
    assert result == {"archived": 4, "kept": 1}

    conn = sqlite3.connect(db)
    got = [r[0] for r in conn.execute(
        "SELECT status FROM properties ORDER BY id")]
    assert got == ["archived", "archived", "archived", "active",
                   "archived", "archived", "active"]
    # the reap leaves an audit trail (dashboard History)
    by, reason, at = conn.execute(
        "SELECT archived_by, archive_reason, archived_at FROM properties "
        "WHERE id = 5").fetchone()
    assert (by, reason) == ("system", "threshold_reap")
    assert at


def test_geo_lookup_index():
    from scraper.geo_lookup import load_index
    geo = load_index()
    assert load_index() is not None
    assert geo.counties_for_zip("27551", "NC") == {"warren"}
    assert "warren" in geo.counties_for_city("macon", "NC")
    assert geo.counties_for_city("macon", "GA") == {"bibb", "monroe", "jones",
                                                    "twiggs"}
    assert geo.counties_for_city("nowhereville", "NC") == set()


def test_geo_resolve_semantics():
    from scraper.geo_lookup import TownCountyIndex
    geo = TownCountyIndex(zips={"NC|27551": ["warren"]},
                          cities={"GA|epworth": ["white"]})
    # unknown town -> keep searched county
    assert geo.resolve("buncombe", "NC", city="Nowhere", zip_code="99999",
                       targets={"buncombe"}) == "buncombe"
    # town in a non-target county -> None (drop the URL county)
    assert geo.resolve("macon", "NC", zip_code="27551",
                       targets={"macon", "buncombe"}) is None
    # town in a different target county -> reassign
    assert geo.resolve("union", "GA", city="Epworth",
                       targets={"union", "white"}) == "white"
    # ambiguous across several target counties -> first hit (the searched
    # county is excluded by the cross-reference, so it can't be kept)
    geo2 = TownCountyIndex(zips={"TN|37374": ["grundy", "marion"]}, cities={})
    assert geo2.resolve("sequatchie", "TN", zip_code="37374",
                        targets={"grundy", "marion",
                                 "sequatchie"}) == "grundy"


def test_parse_geo_rejects_and_reattributes():
    from scraper.geo_lookup import load_index
    geo = load_index()
    # Macon (the town, Warren Co) under a macon-nc search: dropped
    macon = _page([_listing(1, 64900, lot=10.0, state="NC", lat=None, lon=None,
                            address="57 Harbor Dr, Macon, NC 27551",
                            city="Macon", zipcode="27551")])
    assert parse_county_page(macon, "macon", "NC", min_acres=5.0, geo=geo,
                             targets={"macon", "buncombe"}) == []
    # Ellijay (Gilmer) found under a union search: reattributed
    ellijay = _page([_listing(2, 150000, lot=8.0, state="GA",
                              address="100 Costers Creek Rd, Ellijay, GA 30540",
                              city="Ellijay", zipcode="30540")])
    props = parse_county_page(ellijay, "union", "GA", min_acres=5.0, geo=geo,
                              targets={"gilmer", "union", "white"})
    assert [p["county"] for p in props] == ["Gilmer"]
    # without geo the URL county wins (old behavior)
    plain = parse_county_page(ellijay, "union", "GA", min_acres=5.0)
    assert plain[0]["county"] == "Union"


def test_extract_detail():
    cache = json.dumps({"q": {"property": {
        "description": "Creek, views, unrestricted.",
        "parcelId": "066800153806"}}})
    html = _next_data({"props": {"pageProps": {"componentProps": {
        "gdpClientCache": cache}}}})
    desc, parcel = extract_detail(html)
    assert desc == "Creek, views, unrestricted."
    assert parcel == "066800153806"


def test_extract_detail_missing():
    assert extract_detail("") == (None, None)
    assert extract_detail(_next_data({})) == (None, None)


# --------------------------------------------------------------------------
# Parcel harvesting (the number lives in a client-side GraphQL POST)
# --------------------------------------------------------------------------

def test_find_parcel_prefers_the_listings_own_value():
    from scraper.zillow import find_parcel
    payload = {"data": {"property": {
        "zpid": 421685554,
        "parcelNumber": "007.01",
        "nearbySearchResults": {"listResults": [
            {"parcelNumber": "999999999"}]},
    }}}
    assert find_parcel(payload) == "007.01"


def test_find_parcel_skips_history_and_nulls():
    from scraper.zillow import find_parcel
    # tax/price history rows carry their own ids and nulls
    payload = {"data": {"property": {
        "taxHistory": [{"parcelId": None}, {"parcelId": "111222333"}],
        "priceHistory": [{"apn": "444555666"}],
        "apn": "05430t001",
    }}}
    assert find_parcel(payload) == "05430t001"
    # nothing usable at all
    assert find_parcel({"property": {"apn": None, "parcelId": ""}}) is None
    assert find_parcel({"property": {"apn": "N/A"}}) is None
    assert find_parcel({}) is None


def test_extract_detail_finds_nested_attribution_parcel():
    cache = json.dumps({"q": {"property": {
        "description": "Two acres of woods.",
        "attribution": {"parcelNumber": "0668-15-3806"}}}})
    html = _next_data({"props": {"pageProps": {"componentProps": {
        "gdpClientCache": cache}}}})
    desc, parcel = extract_detail(html)
    assert desc == "Two acres of woods."
    assert parcel == "0668-15-3806"


def test_detail_limit_comes_from_config(monkeypatch):
    from scraper import zillow as zl
    monkeypatch.setitem(zl.config.SCRAPER_OVERRIDES["zillow"],
                        "detail_limit", 42)
    assert zl.ZillowScraper().detail_limit == 42
    # an explicit argument still wins
    assert zl.ZillowScraper(detail_limit=7).detail_limit == 7


def test_enrich_details_skips_harvested_rows_and_respects_limit(monkeypatch):
    """Only rows without listing text are rendered, at most detail_limit."""
    from scraper import zillow as zl

    calls = []

    def fake_fetch(self, url):
        calls.append(url)
        zpid = url.split("/")[-2].split("_")[0]
        return ("desc for " + url, None)

    monkeypatch.setattr(zl.ZillowScraper, "_fetch_detail", fake_fetch)
    scraper = zl.ZillowScraper(fetch_details=True,
                               detail_skip={"500", "501"}, detail_limit=3)
    scraper.DELAY_BETWEEN_DETAIL_PAGES = (0, 0)
    props = [{"url": f"https://www.zillow.com/homedetails/{i}_zpid/",
              "source_listing_id": str(i)} for i in range(500, 507)]
    props[5]["description"] = "already has text"      # harvested in an earlier run
    scraper._enrich_details(props)

    assert calls == [f"https://www.zillow.com/homedetails/{i}_zpid/"
                     for i in (502, 503, 504)]
    assert props[0].get("description") is None        # skipped: DB row has text
    assert props[2]["description"] == "desc for " + props[2]["url"]
    assert props[6].get("description") is None        # beyond the cap


class _FakeMouse:
    def __init__(self):
        self.scroll = 0

class _FakeResponse:
    def __init__(self, text=""):
        self._text = text

    def text(self):
        return self._text


class _FakePage:
    """Minimal Playwright page: one HTML response, no scrolling, no XHR."""

    def __init__(self, html=""):
        self.mouse = _FakeMouse()
        self.listeners = []
        self._html = html

    def set_default_timeout(self, ms):
        pass

    def on(self, event, handler):
        self.listeners.append((event, handler))

    def goto(self, url, **kwargs):
        return _FakeResponse(self._html)


def test_fetch_detail_reads_the_ssr_payload_without_waiting(monkeypatch):
    """The parcel POST answers 403 for every automated session, so the page is
    read as served: no scroll, no response listeners, SSR text only."""
    from scraper import zillow as zl

    cache = json.dumps({"q": {"property": {
        "description": "Lake view lot.", "parcelNumber": "066800153806"}}})
    html = _next_data({"props": {"pageProps": {"componentProps": {
        "gdpClientCache": cache}}}})

    pages = []

    class _Ctx:
        def __enter__(self):
            page = _FakePage(html)
            pages.append(page)
            return page

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(zl, "camoufox_context", lambda *a, **k: _Ctx())

    desc, parcel = zl.ZillowScraper()._fetch_detail(
        "https://www.zillow.com/homedetails/1_zpid/")
    assert (desc, parcel) == ("Lake view lot.", "066800153806")
    assert pages[0].mouse.scroll == 0            # no wait for the blocked POST
    assert pages[0].listeners == []


def test_fetch_detail_without_a_payload_returns_nothing(monkeypatch):
    from scraper import zillow as zl

    class _Ctx:
        def __enter__(self):
            return _FakePage("<html>bot wall</html>")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(zl, "camoufox_context", lambda *a, **k: _Ctx())
    assert zl.ZillowScraper()._fetch_detail(
        "https://www.zillow.com/homedetails/1_zpid/") == (None, None)


def test_fetch_detail_swallows_a_failed_page(monkeypatch):
    from scraper import zillow as zl

    class _Boom:
        def __enter__(self):
            raise RuntimeError("browser died")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(zl, "camoufox_context", lambda *a, **k: _Boom())
    assert zl.ZillowScraper()._fetch_detail(
        "https://www.zillow.com/homedetails/1_zpid/") == (None, None)


