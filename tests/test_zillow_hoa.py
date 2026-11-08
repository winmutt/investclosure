"""HOA-fee parsing/verdict tests (pure payloads — no browser).

Calibrated against live Zillow detail payloads (2026-11-07): the fee lives in
``property.monthlyHoaFee`` (number, USD/month) or ``resoFacts.hoaFeeTotal``
("$250 annually"), and occasionally only in the listing description.
2026-11-08 adds the fee-less "HOA yes" rules: the details-card
"Has HOA: Yes" fact (rarely reachable — its GraphQL feed 403s for bots) and
the description-affirmation detector.
"""
import json

from scraper.zillow import (extract_hoa, hoa_affirmed_in_description,
                            hoa_declared, hoa_from_description,
                            parse_hoa_total)
from scraper.zillow_hoa_sweep import verdict_reason


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


class TestHoaAffirmedInDescription:
    """Fee-less HOA existence — every case is a live description (11-08)."""

    def test_active_hoa_is_affirmed(self):
        assert hoa_affirmed_in_description(
            "Located within a gated community with an active HOA ensuring "
            "the neighborhood stays pristine.")

    def test_hoa_website_is_affirmed(self):
        assert hoa_affirmed_in_description(
            "…infrastructure already in place. HOA Website - "
            "https://www.brushcreekhoa.com/index.htm")

    def test_there_is_a_hoa_is_affirmed(self):
        assert hoa_affirmed_in_description(
            "There is a HOA with C & R on both sides of pond and all homes "
            "must be HOA & ARC Approved.")

    def test_part_of_hoa_is_affirmed(self):
        assert hoa_affirmed_in_description(
            "The property is part of an HOA, but covenants only affect the "
            "first 100 feet.")

    def test_community_poa_is_affirmed(self):
        assert hoa_affirmed_in_description(
            "Ownership includes access to the pristine Toccoa River through "
            "the community POA, offering a lifestyle…")

    def test_low_hoa_is_affirmed(self):
        assert hoa_affirmed_in_description(
            "Enjoy the tranquility of nature while benefiting from an "
            "extremely low HOA.")

    def test_hoa_covenants_are_affirmed(self):
        assert hoa_affirmed_in_description(
            "HOA covenants allow lots to be combined, and a main house on "
            "the ridge with guest cottages.")

    def test_denials_are_not_affirmed(self):
        assert hoa_affirmed_in_description("14 ACRES | NO HOA! AND "
                                           "NO RESTRICTIONS") is None
        assert hoa_affirmed_in_description(
            "this unrestricted parcel (NOo HOA) offers 8 wooded acres") is None
        assert hoa_affirmed_in_description(
            "there are no covenants, restrictions or HOA fees as road is "
            "county maintained") is None
        assert hoa_affirmed_in_description(
            "The land is unrestricted with no POA to hamper your choice") is None

    def test_dissolved_or_ditched_hoa_is_not_affirmed(self):
        assert hoa_affirmed_in_description(
            "The HOA/POA has officially been dissolved. Seller is very "
            "motivated!") is None
        assert hoa_affirmed_in_description(
            "Ditch the HOA rules and build your dream mountain getaway") is None

    def test_no_active_poa_is_not_affirmed(self):
        # "no active POA in place" — the word "active" must not win over "no"
        assert hoa_affirmed_in_description(
            "Although there is no active POA in place, the old subdivision "
            "restrictions mandate a minimum 900 sq ft residence") is None

    def test_verify_hedge_is_not_affirmed(self):
        assert hoa_affirmed_in_description(
            "Buyer to verify utilities, zoning, HOA, and requirements.") is None

    def test_neighbour_poa_is_not_affirmed(self):
        assert hoa_affirmed_in_description(
            "On top of wooded seclusion, Powder Horn POA land on two sides "
            "provides added privacy.") is None

    def test_plain_land_copy_is_not_affirmed(self):
        assert hoa_affirmed_in_description(
            "Level lot with long-range views, creek, and paved road frontage.") \
            is None


class TestExtractHoaPresence:
    """The lazily rendered Community & HOA card, when it loads at all."""

    _CARD = ("Community & HOA Community Subdivision: Timberwood Phase Iii "
             "Sect HOA Has HOA: Yes Amenities included: Gated, Park, "
             "Trail(s) HOA fee: $500 annually Location Region: Monteagle")

    def test_card_fee_counts_as_fee(self):
        rec = extract_hoa(_detail({"monthlyHoaFee": None,
                                   "resoFacts": {"hoaFeeTotal": None}}),
                          rendered=self._CARD)
        assert rec["has_fee"] is True
        assert rec["source"] == "details card"
        assert rec["amount"] == 500.0 and rec["period"] == "annually"

    def test_card_yes_without_amount_is_presence(self):
        rec = extract_hoa(
            _detail({"monthlyHoaFee": None, "resoFacts": {}},
                    description="Wooded mountain lot."),
            rendered="Community & HOA HOA Has HOA: Yes Amenities included: "
                     "Trail(s) Location Region:")
        assert rec["has_fee"] is False
        assert rec["hoa_present"] is True
        assert rec["hoa_present_source"] == "details card"

    def test_card_no_beats_affirming_description(self):
        rec = extract_hoa(
            _detail({"monthlyHoaFee": None, "resoFacts": {}},
                    description="The property is part of an HOA."),
            rendered="Community & HOA HOA Has HOA: No Location Region:")
        assert rec["hoa_present"] is False
        assert rec["hoa_absent"] is True

    def test_affirming_description_without_card_is_presence(self):
        rec = extract_hoa(_detail(
            {"monthlyHoaFee": None, "resoFacts": {"hoaFeeTotal": None}},
            description="There is a HOA with C & R."))
        assert rec["has_fee"] is False
        assert rec["hoa_present"] is True
        assert rec["hoa_present_source"] == "description"

    def test_fee_wins_over_presence(self):
        rec = extract_hoa(_detail(
            {"monthlyHoaFee": 21, "resoFacts": {}},
            description="There is a HOA with C & R."))
        assert rec["has_fee"] is True
        assert rec["hoa_present"] is False

    def test_clean_page_has_no_presence(self):
        rec = extract_hoa(_detail(
            {"monthlyHoaFee": None, "resoFacts": {}},
            description="No HOA. Restricted-free mountain acreage."))
        assert rec["has_fee"] is False
        assert rec["hoa_present"] is False
        assert rec["hoa_absent"] is False


class TestHoaDeclaredGuard:
    def test_yes_flag_declares_hoa(self):
        assert hoa_declared({"hoaFee": "Yes"}) is True
        assert hoa_declared({"hoaFee": "yes"}) is True

    def test_amount_is_not_a_declaration(self):
        assert hoa_declared({"hoaFee": "$50 annually"}) is False
        assert hoa_declared({"hoaFee": None}) is False
        assert hoa_declared({}) is False


class TestVerdictReason:
    def test_fee_record_reason(self):
        reason = verdict_reason({"status": "ok", "has_fee": True,
                                 "monthly": 41.67, "amount": 500,
                                 "period": "annually",
                                 "fee_total_raw": "$500 annually",
                                 "source": "monthlyHoaFee"})
        assert reason == ("hoa_fee: $500 annually (zillow detail, "
                          "monthlyHoaFee)")

    def test_presence_record_reason(self):
        reason = verdict_reason({"status": "ok", "has_fee": False,
                                 "hoa_present": True,
                                 "hoa_present_source": "description"})
        assert reason is not None
        assert "HOA yes — no fee amount listed" in reason
        assert "description" in reason

    def test_clean_and_failed_records_archive_nothing(self):
        assert verdict_reason({"status": "ok", "has_fee": False,
                               "hoa_present": False}) is None
        assert verdict_reason({"status": "failed"}) is None
        # old records without the presence field stay untouched
        assert verdict_reason({"status": "ok", "has_fee": False}) is None
