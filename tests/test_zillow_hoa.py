"""HOA-fee parsing/verdict tests (pure payloads — no browser).

Calibrated against live Zillow detail payloads (2026-11-07): the fee lives in
``property.monthlyHoaFee`` (number, USD/month) or ``resoFacts.hoaFeeTotal``
("$250 annually"), and occasionally only in the listing description.
"""
import json

from scraper.zillow import (extract_hoa, hoa_from_description,
                            parse_hoa_total)


def _detail(property_fields: dict, description=None) -> str:
    prop = dict(property_fields)
    if description is not None:
        prop["description"] = description
    cache = {"Property:123": {"property": prop}}
    payload = {"props": {"pageProps": {"componentProps": {
        "gdpClientCache": json.dumps(cache)}}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload) + "</script></html>")


class TestParseHoaTotal:
    def test_monthly_number(self):
        assert parse_hoa_total(21) == (21.0, 21.0, "monthly")
        assert parse_hoa_total("21") == (21.0, 21.0, "monthly")

    def test_annual_string_is_normalised(self):
        monthly, amount, period = parse_hoa_total("$250 annually")
        assert amount == 250.0 and period == "annually"
        assert round(monthly, 2) == round(250 / 12, 2)

        monthly, amount, period = parse_hoa_total("$13/mo")
        assert (amount, period, monthly) == (13.0, "monthly", 13.0)

    def test_zero_and_junk_are_not_fees(self):
        assert parse_hoa_total("$0") is None          # fact-card "$0 HOA fees"
        assert parse_hoa_total(0) is None
        assert parse_hoa_total(0.0) is None
        assert parse_hoa_total(None) is None
        assert parse_hoa_total("") is None
        assert parse_hoa_total(True) is None
        assert parse_hoa_total("Contact agent") is None

    def test_slash_year_is_annual(self):
        monthly, amount, period = parse_hoa_total("$450/year",
                                                  default_period="auto")
        assert (amount, period) == (450.0, "annually")
        assert monthly == 37.5

    def test_unstated_period_in_prose_uses_size(self):
        # "HOA dues are $425" on mountain land -> annual (payload: $35/mo)
        assert round(parse_hoa_total("HOA dues are $425",
                                     default_period="auto")[0], 2) == 35.42
        # a small figure is monthly
        assert parse_hoa_total("HOA dues are $75",
                               default_period="auto")[0] == 75.0
        # the payload's own monthlyHoaFee never gets reinterpreted
        assert parse_hoa_total(450)[0] == 450.0


class TestHoaFromDescription:
    def test_annual_fee_stated_in_prose(self):
        # Live: 4647 Highway 52 #46, Chatsworth GA — both payload fields None.
        fee = hoa_from_description(
            "Underground utilities. HOA Fee is $330 per year. Located between "
            "Chatsworth and Ellijay.")
        assert fee == 27.5

    def test_dues_with_just_phrasing(self):
        fee = hoa_from_description(
            "Low HOA dues of just $150 per year make this affordable.")
        assert fee == 12.5

    def test_price_first_form(self):
        assert hoa_from_description(
            "Quiet community; $50/month association dues apply.") == 50.0

    def test_no_hoa_is_vetoed(self):
        assert hoa_from_description("No HOA fees and no restrictions.") is None
        assert hoa_from_description("Without HOA dues — build when ready.") is None

    def test_bare_mention_is_not_a_fee(self):
        assert hoa_from_description(
            "Looking for a large lot with low HOA fees near the lake?") is None
        assert hoa_from_description("HOA: none") is None
        assert hoa_from_description(None) is None

    def test_bare_integers_are_not_amounts(self):
        # Live false positives (2026-11-07) before the "$" requirement:
        # 0 Shady Bark Ln read "divided into 3" as a $3 fee, and Bearwood Dr
        # read "less than 10 minutes to the lake" as $10.
        assert hoa_from_description(
            "There are HOA fees but if divided into 3, the HOA fee would be "
            "$2100 annually.") == 175.0
        assert hoa_from_description(
            "Low HOA fees that is less than 10 minutes to Lake Glenville.") is None

    def test_slash_year_and_unstated_period(self):
        assert hoa_from_description(
            "Affordable: $450/year HOA maintains the roads.") == 37.5
        assert round(hoa_from_description("HOA dues are $425."), 2) == 35.42


class TestExtractHoa:
    def test_monthly_field_wins(self):
        rec = extract_hoa(_detail({"monthlyHoaFee": 21,
                                   "resoFacts": {"hoaFeeTotal": "$250 annually"}}))
        assert rec["has_fee"] is True
        assert rec["source"] == "monthlyHoaFee"
        assert rec["monthly"] == 21.0

    def test_falls_back_to_hoa_fee_total(self):
        rec = extract_hoa(_detail({"monthlyHoaFee": None,
                                   "resoFacts": {"hoaFeeTotal": "$250 annually"}}))
        assert rec["has_fee"] is True
        assert rec["source"] == "hoaFeeTotal"
        assert round(rec["monthly"], 2) == 20.83

    def test_description_only_fee(self):
        rec = extract_hoa(_detail(
            {"monthlyHoaFee": None, "resoFacts": {"hoaFeeTotal": None}},
            description="HOA Fee is $330 per year."))
        assert rec["has_fee"] is True
        assert rec["source"] == "description"
        assert rec["description_fee"] == 27.5

    def test_no_fee_anywhere(self):
        rec = extract_hoa(_detail(
            {"monthlyHoaFee": None, "resoFacts": {"hoaFeeTotal": None}},
            description="Level lot with long-range views. No HOA fees."))
        assert rec["has_fee"] is False
        assert rec["monthly"] is None
        assert rec["description_fee"] is None

    def test_missing_payload_returns_none(self):
        assert extract_hoa("<html>Access Denied</html>") is None
        assert extract_hoa("") is None
