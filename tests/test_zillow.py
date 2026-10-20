"""Tests for the ported Zillow scraper (pure parsing — no browser)."""
import json

from scraper.zillow import (
    build_county_url, county_slug, county_display, lot_acres,
    parse_county_page, extract_detail,
)


def _next_data(payload: dict) -> str:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'


def _listing(zpid, price, lot=None, unit="acres", state="NC",
             address="1 Empty Road, Sparta, NC 28675", home_type="LOT",
             lat=36.5, lon=-81.1):
    home_info = {"price": price, "state": state, "homeType": home_type,
                 "streetAddress": address}
    if lot is not None:
        home_info["lotAreaValue"] = lot
        home_info["lotAreaUnit"] = unit
    return {
        "zpid": zpid,
        "address": address,
        "unformattedPrice": price,
        "detailUrl": f"https://www.zillow.com/homedetails/{zpid}_zpid/",
        "addressCity": "Sparta",
        "addressZipcode": "28675",
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
        "https://www.zillow.com/homes/van-buren-tn/"


def test_lot_acres_units():
    assert lot_acres({"lotAreaValue": 5, "lotAreaUnit": "acres"}) == 5
    assert lot_acres({"lotAreaValue": 43560, "lotAreaUnit": "sqft"}) == 1.0
    assert lot_acres({}) is None
    assert lot_acres({"lotAreaValue": "junk", "lotAreaUnit": "acres"}) is None


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
        _listing(3, 100000, lot=None),        # unknown acreage kept
        _listing(3, 120000, lot=8.0),         # dup zpid
        _listing(4, 90000, lot=87120, unit="sqft"),  # 2 acres via sqft
    ]
    props = parse_county_page(_page(items), "ashe", "NC",
                              max_price=400000, min_acres=1.1)
    ids = [p["source_listing_id"] for p in props]
    assert ids == ["3", "4"]
    assert props[0]["acres"] is None
    assert props[1]["acres"] == 2.0


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
