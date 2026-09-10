"""Trustee-sale (mortgage foreclosure) scrapers: shared helpers + row parsers."""
from __future__ import annotations

from scraper.trustee_base import (
    clean_html,
    parse_sale_date,
    price_to_dollars,
    split_address,
)


class TestHelpers:
    def test_clean_html(self):
        assert clean_html("<b>123 Main</b> St") == "123 Main St"

    def test_price_to_dollars(self):
        assert price_to_dollars("$228,616.00") == 228616.0
        assert price_to_dollars("$0.00") is None
        assert price_to_dollars("") is None
        assert price_to_dollars("Call for bid") is None

    def test_parse_sale_date(self):
        assert parse_sale_date("10/06/2026 (10am - 4pm)") == "2026-10-06"
        assert parse_sale_date("07/29/2026 \u00b7 2:00 PM") == "2026-07-29"
        assert parse_sale_date("09/17/26") == "2026-09-17"
        assert parse_sale_date("11/12/2025") == "2025-11-12"
        assert parse_sale_date("October 7, 2026") == "2026-10-07"
        assert parse_sale_date("") is None
        assert parse_sale_date("TBD") is None

    def test_split_address(self):
        assert split_address(
            "1306 Shellbark Ct Havelock, North Carolina 28532",
            "North Carolina") == ("1306 Shellbark Ct", "Havelock", "28532")
        assert split_address(
            "611 West 26 St Winston Salem, North Carolina 27105",
            "North Carolina") == ("611 West 26 St", "Winston Salem", "27105")
        assert split_address("123 Main, Blairsville", "") == (
            "123 Main", "Blairsville", None)


class TestRLSelaw:
    def _s(self):
        from scraper.rlselaw import RLSelawScraper
        return RLSelawScraper.__new__(RLSelawScraper)

    def test_mountain_row_kept(self):
        prop, reason = self._s()._parse_cells(
            ["11/04/2026 (10am - 4pm)", "26-00001", "123 Mountain Rd",
             "Blairsville", "30512", "Union", "Go to Auction.com"],
            today="2026-01-01")
        assert reason is None
        assert prop["property_type"] == "mortgage_foreclosure"
        assert prop["state"] == "GA"
        assert prop["county"] == "Union"
        assert prop["auction_date"] == "2026-11-04"
        assert prop["source_listing_id"] == "26-00001"

    def test_non_target_county_skipped(self):
        prop, reason = self._s()._parse_cells(
            ["10/06/2026 (10am - 4pm)", "26-01105", "1335 County Line Rd Nw",
             "Acworth", "30101", "Cobb", "Go to servicelinkauction.com"],
            today="2026-01-01")
        assert prop is None and reason == "county"

    def test_past_sale_skipped(self):
        prop, reason = self._s()._parse_cells(
            ["11/04/2026 (10am - 4pm)", "26-00001", "123 Mountain Rd",
             "Blairsville", "30512", "Union", "Go to Auction.com"],
            today="2027-01-01")
        assert prop is None and reason == "past"


class TestBrockScott:
    def _s(self):
        from scraper.brockandscott import BrockScottScraper
        return BrockScottScraper.__new__(BrockScottScraper)

    def test_mountain_row_kept(self):
        from scraper.config import NC_MOUNTAIN_COUNTIES
        counties = {c.lower() for c in NC_MOUNTAIN_COUNTIES}
        prop, dropped = self._s()._parse_cells(
            ["Buncombe", "10/15/2026 \u00b7 11:00 AM", "NC", "26SP000183-100",
             "26-00330-FC01", "18 Olive St Asheville, North Carolina 28801",
             "$228,616.00", "3800/1076"],
            "NC", "North Carolina", counties, "http://x", "2026-01-01")
        assert dropped is False
        assert prop["property_type"] == "mortgage_foreclosure"
        assert prop["court_case"] == "26SP000183-100"
        assert prop["price"] == 228616.0
        assert prop["city"] == "Asheville"
        assert prop["auction_date"] == "2026-10-15"

    def test_non_target_and_past_dropped(self):
        from scraper.config import NC_MOUNTAIN_COUNTIES
        counties = {c.lower() for c in NC_MOUNTAIN_COUNTIES}
        prop, dropped = self._s()._parse_cells(
            ["Craven", "07/29/2026 \u00b7 2:00 PM", "NC", "26SP000012-240",
             "26-00330-FC01", "1306 Shellbark Ct Havelock, North Carolina 28532",
             "$228,616.00", "3800/1076"],
            "NC", "North Carolina", counties, "http://x", "2026-01-01")
        assert prop is None and dropped is True


class TestForeclosureTennessee:
    def _s(self):
        from scraper.foreclosuretennessee import ForeclosureTennesseeScraper
        return ForeclosureTennesseeScraper.__new__(ForeclosureTennesseeScraper)

    BODY = ("Listing Details\nSubmission ID: 999\nPosted: 09/01/2026\n"
            "Sale Date: 12/15/2029\nTrustee Name: Test Trustee, PLLC\n"
            "Property Address: 495 Emmett Rd\nCity/State: Blountville, TN\n"
            "Zipcode: 37617\nCounty: Sullivan\nNotice Content sale text here")

    def test_mountain_detail_kept(self):
        rec = {"link": "http://x?a=1", "county": "Sullivan",
               "address": "495 Emmett Rd", "city": "Blountville",
               "zip": "37617", "sale": "12/15/2029", "cont": "",
               "firm": "Test Trustee"}
        prop = self._s()._parse_detail(self.BODY, rec)
        assert prop["property_type"] == "mortgage_foreclosure"
        assert prop["state"] == "TN"
        assert prop["county"] == "Sullivan"
        assert prop["auction_date"] == "2029-12-15"
        assert prop["source_listing_id"] == "ftn:999"

    def test_past_sale_dropped(self):
        body = self.BODY.replace("12/15/2029", "01/10/2020")
        rec = {"link": "http://x", "county": "Sullivan", "address": "a",
               "city": "b", "zip": "c", "sale": "", "cont": "", "firm": ""}
        assert self._s()._parse_detail(body, rec) is None


class TestBellCarrington:
    def test_rows_filtered_and_typed(self):
        from scraper.bellcarrington import BellCarringtonScraper
        s = BellCarringtonScraper.__new__(BellCarringtonScraper)
        rows = [
            {"state": "GA", "county": "Union",
             "property": "123 Mountain Rd", "city": "Blairsville",
             "zip": "30512", "sale date": "11/04/2029",
             "bid": "$50,000", "notes": ""},
            {"state": "", "county": "GA",
             "property": "", "city": "",
             "zip": "", "sale date": "",
             "bid": "", "notes": ""},
            {"state": "", "county": "Towns",
             "property": "456 Lake Rd", "city": "Hiawassee",
             "zip": "30546", "sale date": "12/01/2029",
             "bid": "TBA", "notes": ""},
            {"State": "GA", "County": "Cobb",
             "Property Address": "1 Main St", "City": "Marietta",
             "Zip": "30060", "Sale Date": "11/04/2029",
             "Opening Bid": "$50,000", "Case Number": "26-2"},
        ]
        props = s._parse_rows(rows)
        assert len(props) == 2
        assert props[0]["property_type"] == "mortgage_foreclosure"
        assert props[0]["state"] == "GA"
        assert props[1]["county"] == "Towns"  # state inherited from section


class TestLogsNC:
    def test_mountain_row_kept(self):
        from scraper.logs_nc import LogsNCScraper
        s = LogsNCScraper.__new__(LogsNCScraper)
        prop = s._parse_row({
            "county": "Buncombe", "sale": "12/15/29", "time": "11:00 AM",
            "case": "21SP000183-100",
            "address": "18 Olive St, Asheville, North Carolina 28801",
            "bid": ""})
        assert prop is not None
        assert prop["property_type"] == "mortgage_foreclosure"
        assert prop["court_case"] == "21SP000183-100"
        assert prop["city"] == "Asheville"

    def test_non_target_skipped(self):
        from scraper.logs_nc import LogsNCScraper
        s = LogsNCScraper.__new__(LogsNCScraper)
        assert s._parse_row({
            "county": "Anson", "sale": "09/10/26", "time": "",
            "case": "26SP000018-030", "address": "65 Maynard Street",
            "bid": "40000"}) is None
