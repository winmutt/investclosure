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


def test_all_four_registered():
    from scraper.run import SCRAPER_MODULES
    for name in ("ashe_county", "haywood_county", "macon_county", "mcdowell_county"):
        assert name in SCRAPER_MODULES, f"{name} not registered"
