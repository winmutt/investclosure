"""Tests for the NC county static-page scrapers (ashe/haywood/macon/mcdowell).

Live page fetches are NOT tested here (camoufox + network); these cover
the pure parsing helpers in county_static and the McDowell row parser.
"""
import re

from scraper import county_static as cs
from scraper.mcdowell_county import McDowellCountyScraper
from scraper.ashe_county import ROW_RE


def test_parse_money():
    assert cs.parse_money("$ 15,750.00") == 15750.00
    assert cs.parse_money("opening bid $9,535.00") == 9535.00
    assert cs.parse_money("Call Clerk of Court") is None
    assert cs.parse_money("") is None


def test_find_case():
    assert cs.find_case("FILE NUMBER 26CV000538-580") == "26CV000538-580"
    assert cs.find_case("26 SP 000067-050") == "26SP000067-050"
    assert cs.find_case("no case here") is None


def test_to_iso_date():
    assert cs.to_iso_date("08/07/2026") == "2026-08-07"
    assert cs.to_iso_date("September 25, 2026") == "2026-09-25"
    assert cs.to_iso_date("TBA") is None


def test_updated_date():
    assert cs.updated_date("ASHE COUNTY, NC PROPERTY TAX FORECLOSURE Updated : 04/27/2018") == "2018-04-27"
    assert cs.updated_date("no stamp here") is None


def test_content_hash_stable():
    assert cs.content_hash("None at this time.") == cs.content_hash("  none   AT this time. ")
    assert cs.content_hash("a") != cs.content_hash("b")


def test_mcdowell_upset_row_splits_parcels():
    scraper = McDowellCountyScraper()
    headers = ["ORIGINAL SALE DATE", "10-DAY UPSET BID PERIOD ENDS",
               "PARCEL NUMBER", "HIGHEST BID RECORDED", "FILE NUMBER"]
    row = ["08/07/2026", "09/28/2026", "1739-00-31-2535 / 1739-00-21-7533",
           "$ 15,750.00", "26CV000538-580"]
    props = scraper._parse_row("upset", headers, row)
    assert len(props) == 2
    parcels = sorted(p["parcel_number"] for p in props)
    assert parcels == ["1739-00-21-7533", "1739-00-31-2535"]
    for p in props:
        assert p["price"] == 15750.00
        assert p["court_case"] == "26CV000538-580"
        assert p["auction_date"] == "2026-09-28"
        assert p["upset_bid"] == "$15,750.00"
        assert p["county"] == "McDowell"
        assert p["property_type"] == "tax_foreclosure"


def test_mcdowell_empty_row_skipped():
    scraper = McDowellCountyScraper()
    headers = ["SALE DATE", "SALE TIME", "PARCEL NUMBER", "OPENING BID AMOUNT", "FILE NUMBER"]
    assert scraper._parse_row("upcoming", headers, ["NO FORECLOSURE SALES SCHEDULED AT THIS TIME"]) == []


def test_mcdowell_parcel_cell_split():
    from scraper.mcdowell_county import _split_parcels
    assert _split_parcels("1739-00-31-2535 / 1739-00-21-7533") == [
        "1739-00-31-2535", "1739-00-21-7533"]
    assert _split_parcels("NO PENDING FORECLOSURES AT THIS TIME") == []


def test_ashe_row_regex_sample():
    line = ("13190 307 045 Crown Spruce Lane West Jefferson 1.011 acre "
            "subdivision homesite TBA TBA $9,535.00")
    m = ROW_RE.search(line)
    assert m is not None
    assert re.sub(r"\s+", "", m.group("parcel")) == "13190307045"
    assert cs.parse_money(m.group("bid")) == 9535.00


def test_find_parcels_rejects_zip_and_phone():
    assert cs.find_parcels("Franklin, NC 28734 - 3005 Phone: (828) 349-2148") == []
    assert cs.find_parcels("PARCEL NUMBER 1739-00-31-2535 / 1739-00-21-7533") == [
        "1739-00-31-2535", "1739-00-21-7533"]
    assert cs.find_parcels("Parcel #1984-32-8523-000") == ["1984-32-8523-000"]


def test_enrich_and_filter_acres_drops_known_below_min(monkeypatch):
    from scraper import county_static
    def fake_fill(props, rate=0.6):
        props[0]["acres"] = 0.88
        props[0]["acres_source"] = "gis"
        return {"enriched": 1, "failed": 0}
    monkeypatch.setattr("scraper.nc_gis_lookup.fill_acres_in_memory",
                        fake_fill, raising=False)
    props = [
        {"source_listing_id": "a", "county": "McDowell", "state": "NC",
         "parcel_number": "1739-00-31-2535", "acres": None, "url": "u"},
        {"source_listing_id": "b", "county": "McDowell", "state": "NC",
         "parcel_number": "9999", "acres": 4.5, "url": "u"},
        {"source_listing_id": "c", "county": "McDowell", "state": "NC",
         "parcel_number": "8888", "acres": None, "url": "u"},
    ]
    kept = county_static.enrich_and_filter_acres("x_test", props)
    ids = [p["source_listing_id"] for p in kept]
    # "a" got 0.88ac from the fill and was dropped below MIN_ACRES;
    # "b" (known 4.5) and "c" (still unknown) stay.
    assert ids == ["b", "c"]
    assert props[0]["acres"] == 0.88  # fill ran pre-filter (drop reason)


def test_trustee_apply_acre_policy(monkeypatch):
    from scraper.trustee_base import TrusteeSaleScraper
    monkeypatch.setattr("scraper.nc_gis_lookup.fill_acres_in_memory",
                        lambda props, rate=0.6: {"enriched": 0, "failed": 0},
                        raising=False)

    class _S(TrusteeSaleScraper):
        SOURCE_NAME = "t_test"

        def scrape(self):
            return []

    props = [
        {"source_listing_id": "x", "county": "Buncombe", "state": "NC",
         "acres": 0.5},
        {"source_listing_id": "y", "county": "Buncombe", "state": "NC",
         "acres": 2.0},
        {"source_listing_id": "c", "county": "Buncombe", "state": "NC",
         "acres": None},
    ]
    kept = _S().apply_acre_policy(props)
    assert [p["source_listing_id"] for p in kept] == ["y", "c"]


def test_all_four_registered():
    from scraper.run import SCRAPER_MODULES
    for name in ("ashe_county", "haywood_county", "macon_county", "mcdowell_county"):
        assert name in SCRAPER_MODULES, f"{name} not registered"


def test_haywood_notice_parsers_real_text():
    """Fields parsed from the real bidID=267 notice (OCR'd 2026-10-20)."""
    from scraper.haywood_county import (
        _extract_acreage, _extract_address,
    )
    from scraper import county_static as cs
    notice = (
        "IN THE GENERAL COURT OF JUSTICE\n26CV000653-430\n"
        "NOTICE OF TAX FORECLOSURE SALE\n"
        'County of Haywood vs. 597 Laure] Branch, LLC (Owner), et al."\n'
        "sell for cash, to the last and highest bidder at public auction\n"
        'shown on the Haywood County Tax Records as Parcel Identification\n'
        'Number ("PIN") 8711-00-6993, and is that certain parcel, tract or\n'
        "Lot No. 20-T Revised of Twin Oaks Estates, Section One, containing 1.21 acres\n"
        "The real property to be sold is generally described* as 597 Laurel Branch Road,\n"
        "Waynesville, NC 28785."
    )
    assert cs.find_case(notice) == "26CV000653-430"
    assert cs.find_parcels(notice) == ["8711-00-6993"]
    assert _extract_acreage(notice) == 1.21
    assert _extract_address(notice) == "597 Laurel Branch Road"
    # Attorney-firm footer address must never win
    assert _extract_address("11 N. Market Street, Asheville, NC 28801 (828)258-2991") is None


def test_haywood_address_requires_three_words():
    from scraper.haywood_county import _extract_address
    assert _extract_address("described as 34.72 feet, Waynesville, NC 28786") is None
    assert _extract_address(
        "described* as 597 Laurel Branch Road Waynesville NC") is None  # no city comma


def test_haywood_detail_body_fallback_parses_labels():
    from scraper.haywood_county import _parse_detail_body_fallback
    body = (
        "HomeBid Postings\n"
        "\tPrint\n"
        "Bid Title:\tTax Foreclosure sale November 19th @10:00 a.m.\n\n"
        "Category:\tTax Foreclosures\n"
        "Status:\tOpen\n\n"
        "Description:\n\n\n"
        "/DocumentCenter/View/8001/597-Laurel-Branch-LLC\n\n\n"
        "Publication Date/Time:\n10/13/2026 10:00 AM\n"
        "Publication Information:\n"
        "Tax Foreclosure Sale November 19th @ 10:00 a.m/\n"
        "Closing Date/Time:\n11/19/2026 10:15 AM\n"
    )
    fields = _parse_detail_body_fallback(body)
    assert fields["Bid Title"] == "Tax Foreclosure sale November 19th @10:00 a.m."
    assert fields["Closing Date/Time"] == "11/19/2026 10:15 AM"
    assert fields["Status"] == "Open"


def test_haywood_notice_acreage_policy():
    from scraper.haywood_county import HaywoodCountyScraper
    s = HaywoodCountyScraper()
    # GIS already resolved row keeps GIS acres; fallback only when GIS None.
    props = [
        {"source_listing_id": "g", "county": "Haywood", "state": "NC",
         "acres": 1.0, "acres_source": "gis", "raw_source_text": "", "url": "u"},
        {"source_listing_id": "f", "county": "Haywood", "state": "NC",
         "acres": None, "raw_source_text": "", "url": "u"},
        {"source_listing_id": "sub", "county": "Haywood", "state": "NC",
         "acres": None, "raw_source_text": "", "url": "u"},
    ]
    notice = {"g": None, "f": 1.21, "sub": 0.5}
    kept = s._apply_notice_acres(props, notice)
    ids = [p["source_listing_id"] for p in kept]
    assert ids == ["g", "f"]  # 'sub' dropped: 0.5ac notice figure < MIN_ACRES
    assert props[0]["acres"] == 1.0 and props[0]["acres_source"] == "gis"
    assert props[1]["acres"] == 1.21 and props[1]["acres_source"] == "notice_text"


def test_haywood_bid_ids_extracted_from_list_html():
    from scraper.haywood_county import BID_ID_RE
    html = (
        '<a href="/bids.aspx?bidID=267">Tax Foreclosure sale November 19th</a>'
        '<a href="/bids.aspx?bidID=267&PRINT=YES">Print</a>'
        '<a href="/bids.aspx">Return To Main Bid Postings Page</a>'
        '<a href="/bids.aspx?bidID=300">Another sale</a>'
    )
    found = BID_ID_RE.findall(html)
    seen: set = set()
    ordered = [b for b in found if not (b in seen or seen.add(b))]
    assert ordered == ["267", "300"]
