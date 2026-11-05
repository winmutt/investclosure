"""Tests for the ported Zillow scraper (pure parsing — no browser)."""
import json

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
    root = "https://www.zillow.com/homes/for_sale/"
    # page 1 has no _p/ segment; the -county- slug is preserved
    assert build_paged_county_url("macon", "NC") == \
        root + "macon-county-nc/"
    assert build_paged_county_url("van_buren", "TN", page=3) == \
        root + "van-buren-county-tn/3_p/"
    # price cap becomes the legacy inline filter (the only form that works)
    assert build_paged_county_url("macon", "NC", page=12, max_price=250000) == \
        root + "macon-county-nc/0-250000_price/12_p/"
    # no cap -> no price segment (0 means "no cap", never "0-0")
    assert build_paged_county_url("ashe", "NC", max_price=0) == \
        root + "ashe-county-nc/"


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
        assert "0-250000_price" in url, url
        assert (f"/{page_no}_p/" in url) == (page_no > 1), url
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
