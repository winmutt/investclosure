"""Tests for scraper.building_detect — existing-structure classifier.

Covers the marketing-copy traps that drove the design: dream-home language
on vacant land, cross-sold neighbour homes, zoning copy, and the
uninhabitable-structure vocabulary (as-is / destroyed / tear-down) that
Zillow never feeds as bed/bath facts. Fixtures mirror the live GA lot
descriptions the patterns were calibrated against.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scraper.building_detect import (
    AMBIGUOUS, HAS_BUILDING, NO_BUILDING, classify_listing,
    description_verdict, existing_evidence, insights_verdict, needs_images,
    notice_evidence, notice_verdict)

HAS = [
    "The well-maintained, older manufactured home sits on 8 acres with creek.",
    "Small cabin on property, sold as is where is, needs work.",
    "Uninhabitable house, fire damaged roof, concrete foundation remains.",
    "Cozy retreat! Kitchen updated, 2 bedrooms, fireplace, wrap around porch.",
    "Older double-wide on .5 acre lot, as-is.",
    "No HOA and no restrictions, has a rustic one-room cabin with wood stove.",
    "Formerly a residence lot, cleared in 2019, one foundation slab remains.",
    "As-is parcel with old single wide on permanent foundation.",
    "Property includes a two-bedroom cabin and separate barn.",
    "The home was destroyed by fire; slab and chimney remain.",
    "No HOA, and a barn on site makes it perfect.",
    ("There is a mobile home and a carport on the property - both have "
     "storm damage and need to be mitigated/refurbished or torn down."),
    ("An existing storage building with a metal roof includes previously "
     "installed bathroom plumbing."),
    "The concrete-block structure behind the home may need to be removed.",
    "Property includes an old shack that is considered a tear-down (do not enter).",
    "Offering 5 acres with an old cabin, sold together.",
]

NO = [
    "Almost 10 acres, room for several homes, unground power.",
    "Your dream home awaits on this wooded lot with multiple building sites.",
    "Great vacant lot, no structures, bring your builder.",
    "Beautiful land with a small home site cleared near the creek.",
    "Level cleared lot ready for your home. Power at road.",
    "Lot sold with no existing structures or improvements.",
    "No mobile homes permitted in this subdivision. Build yours today.",
    "The property does not have a house but has a new gravel driveway.",
    "Perfect for a tiny home or camper pad. No restrictions.",
    "Unrestricted by an existing structure, this is true raw land.",
    "You can put a mobile home on it.",
    "Also available in combination with a home on 5 adjoining acres.",
    "Along with this lot, a home on 5.2 acres is offered separately.",
    "Two homes are allowed on this lot. No mobile homes.",
    "Homes of 800+ square feet permitted in the community.",
    "Currently zoned agricultural and ready for your new residence.",
    "Equally suited for a full-time residence or investment.",
    "Build your dream home next to the creek on this private lot.",
    "Vacant land, no house, no cabin, just mountains.",
    "Bring your builder! Cleared building site with water hookups.",
]

AMBIG = [
    "Gorgeous mountain views, creek, privacy. 5 wooded acres.",
    "Room to build, with utilities and a septic already in place.",
    "Sold as is, price firm, by owner. Two parcel assemblage.",
    "Seller will install a 4 bedroom septic tank with full price offer.",
    "",
    None,
]


class TestDescriptionVerdict:
    @pytest.mark.parametrize("text", HAS)
    def test_existing_structure_detected(self, text):
        assert description_verdict(text) == HAS_BUILDING

    @pytest.mark.parametrize("text", NO)
    def test_future_or_vacant_copy_not_flagged(self, text):
        assert description_verdict(text) == NO_BUILDING

    @pytest.mark.parametrize("text", AMBIG)
    def test_unresolved_copy_stays_ambiguous(self, text):
        assert description_verdict(text) == AMBIGUOUS

    def test_evidence_window_is_quotable(self):
        ev = existing_evidence(HAS[11])
        assert ev and "storm damage" in ev[0].lower()


class TestClassifyListing:
    def test_living_area_fact_wins(self):
        assert classify_listing("5 wooded acres of privacy",
                                prop={"livingAreaValue": 1200}) \
            == HAS_BUILDING

    def test_year_built_fact(self):
        assert classify_listing("nice lot", prop={"resoFacts":
                                                  {"yearBuilt": 1968}}) \
            == HAS_BUILDING

    def test_interior_insight_phrase(self):
        insights = [{"insights": [{"phrases": ["Cozy wood-burning fireplace"]}]}]
        assert classify_listing("Beautiful 5 acres, mountain views", insights) \
            == HAS_BUILDING

    def test_marketing_insight_home_word_not_structure(self):
        insights = [{"insights": [{"phrases": ["Perfect spot for your home"]}]}]
        assert not insights_verdict(insights)


class TestNeedsImages:
    def test_ambiguous_always(self):
        assert needs_images(AMBIGUOUS, 0)

    def test_vacant_only_with_big_gallery(self):
        assert not needs_images(NO_BUILDING, 3)
        assert needs_images(NO_BUILDING, 6)

    def test_has_building_never(self):
        assert not needs_images(HAS_BUILDING, 30)


class TestSentenceAwareness:
    """Regression cases from the GA calibration + live NC audit."""

    @pytest.mark.parametrize("text", [
        # "the property sits…" is land copy, not a dwelling that sits.
        "The property sits on 1.2 acres; build your dream home on the pad.",
        "Whether you build a cottage or park a trailer, this lot delivers.",
        "Great spot where your dream home could sit, perked and cleared.",
        # cross-sell / spec copy
        "Many homes next to the acreage.",
        "Room for a manufactured home on a level lot.",
        # the manufactured home is LEAVING, not part of the sale's use
        "Buy a piece of land with a manufactured home waiting to be moved off.",
        "Two manufactured homes on land for sale, move them or remove them.",
    ])
    def test_land_copy_never_asserts_building(self, text):
        assert description_verdict(text) != HAS_BUILDING

    @pytest.mark.parametrize("text", [
        "A one-room cabin with electricity sits below the driveway.",
        "An old cabin sits on the site, power at the pole.",
        # strong presence markers beat a removal note in the same sentence
        ("All utilities at the property, existing 1998 single wide "
         "manufactured home on slab, removal required by county."),
        "Existing manufactured home built in 2004, needs work.",
    ])
    def test_placement_sentence_still_asserts(self, text):
        assert description_verdict(text) == HAS_BUILDING

    def test_up_to_bedroom_is_spec_not_room(self):
        assert description_verdict(
            "Build your dream home with up to 3-bedroom on this homesite. "
            "No known storm damage and septic confirmed. Bring your plans."
        ) != HAS_BUILDING

    def test_no_known_storm_damage_negates(self):
        assert description_verdict(
            "Level cleared lot. No known storm damage on the property.") \
            != HAS_BUILDING

    def test_blank_slate_no_structure_word_settles_vacant(self):
        assert description_verdict(
            "Blank slate! Nothing but trees and a gravel road.") \
            == NO_BUILDING

    def test_weak_future_without_token_stays_ambiguous(self):
        assert description_verdict(
            "Room to build, with utilities and a septic already in place.") \
            == AMBIGUOUS

    def test_building_site_markers_vacant(self):
        assert description_verdict("Mountain building site with lake view.") \
            == NO_BUILDING
        assert description_verdict("Cleared home site, ready to build.") \
            == NO_BUILDING


# ---------------------------------------------------------------------------
# Notice text (non-Zillow sources) — snippets quoted from live active rows
# ---------------------------------------------------------------------------
# The 2026-11-11 audit of the active notice inventory: the Zillow-path
# collectors promoted 31 of 184 rows and 14 were boilerplate, a sale venue or a
# display ad. These fixtures keep both halves of that result.

NOTICE_HAS = [
    # AL — the manufactured home is part of what is conveyed
    ("Blount AL 877", "Situated in Blount County, Alabama. Together with a "
     "multiwide manufactured home which Is permanently affixed and attached "
     "to the land and is part of the real property."),
    ("Talladega AL 1141", "Lot 39 Book 3 Page 162 Parcel No. 03-05-22-3-005-"
     "039.000 ALSO, one (1) 2003 Cavalier 80' x 28' manufactured home, "
     "Serial No. CV03AL0260562AB; together with the hereditaments."),
    # TN — trustee notices titled SALE OF REAL ESTATE AND AFFIXED HOME
    ("Knox TN 503", "TRUSTEE'S NOTICE OF SALE OF REAL ESTATE AND AFFIXED "
     "MANUFACTURED HOME. Pursuant to T.C.A. §47-9-604 this sale shall also "
     "include the affixed manufactured home described as one (1) 2001 CMH "
     "Manufactured Home bearing Serial No. FL58X1409285."),
    ("Warren TN 541", "ALSO CONVEYED IS A 1998 OAKWOOD MOBILE HOME, MODEL "
     "FRE, BEARING VIN NO. H0NC02233152AB WHICH HAS BECOME a fixture."),
    ("Polk TN 520", "Also conveyed herewith, a 2001 Jagu Jag Manufactured "
     "Home VIN No. GMHGA4190128142AB, Title No. 64760524."),
    # KY — newspaper columns interleave, but the mobile home is the subject
    ("Harlan KY 1223", "the mobile home situated on and attorney fees, until "
     "paid SERVICING, LLC, THE HEREIN DESCRIBED estate located in Harlan "
     "County, COMMISSIONER'S SALE."),
    # REO/trustee fact blocks (auction_com publishes the MLS fields)
    ("Blount AL 5830", "Square Footage 1782 Lot Size (Acres) 19 Property "
     "Type Single Family... Year Built 1994 File No 26-02851-LC-A."),
    ("Monroe TN 5798", "Square Footage 2356 Lot Size (Acres) 9.21 Property "
     "Type Single Family... Year Built 1988 Occupied: Do not disturb "
     "occupants. Trespassing is a criminal offense."),
    # a genuine conveyance survives a venue sentence elsewhere in the notice
    ("Sullivan TN 542", "CONVEYED UNTO THE GRANTORS, A 55 X 27, 1999 HOMES "
     "OF LEGEND, INC. MOBILE HOME, MODEL #: AL L1408SB, SERIAL #: HL 58795 "
     "A, WITH A VIN # OF NTA998476998477. Sale will be held at the front of "
     "the Circuit Court building for the County of Washington located at "
     "189 E. Main Street, Abingdon, Virginia 24210."),
]

NOTICE_NOT_EVIDENCE = [
    # the AS-IS disclaimer appears on vacant parcels too
    ("NC as-is clause 5530", "The Trustee under Deed of Trust will offer the "
     "Property for sale, transfer and conveyance \"AS IS WHERE IS.\" There "
     "are no representations of warranty relating to the condition of the "
     "Property or improvements."),
    # HUD reverse-mortgage recital
    ("NC HUD clause 1129", "should the Property not remain the principal "
     "residence of at least one surviving Borrower, the Secretary has the "
     "right to foreclose this security instrument."),
    # the sale venue, not the property
    ("AL courthouse 884", "Sale will be held for cash at the front steps to "
     "the DeKalb county court- house located at 300 Grand Avenue SW, Fort "
     "Payne, AL 35967 in Fort Payne, Alabama."),
    # third-party display ads sharing a newspaper column
    ("KY apartment ad 1159", "APARTMENTS APARTMENTS deposit test. Duties "
     "include We Buy Houses for Cash AS antee and lifetime transfer-."),
    ("KY watch/rental ad 1159", "Let us make your tona, GMT, Submariner and "
     "3 bedrooms, 1 Applicants must be internet included. $2,200 per month."),
    # deed boilerplate: conveys whatever may or may not stand there
    ("NC improvements clause 5600", "Together with improvements located "
     "hereon; said property being located at 1877 Chandler Creek Road, "
     "Mars Hill, NC 28754 Tax ID: 9759-37-9558."),
    ("NC improvements if any", "Being further subject to all prior sales and "
     "releases and to all deeds of trust, liens, unpaid taxes, matters of "
     "record, and improvements, if any."),
    # FinCEN residential-real-estate reporting clause
    ("FinCEN recital", "This report is filed under the FinCEN residential "
     "real estate reporting rule; the transferee is a legal entity."),
    # nothing at all — a metes-and-bounds description
    ("metes only", "Beginning at a iron pin on the west line of the highway, "
     "thence North 42 degrees West 180 poles to a stone, thence South 50 "
     "poles to the beginning."),
]


class TestNoticeEvidence:
    @pytest.mark.parametrize("name,text", NOTICE_HAS)
    def test_conveyed_structure_is_evidence(self, name, text):
        quotes = notice_evidence(text)
        assert quotes, name
        assert notice_verdict(text) == HAS_BUILDING

    @pytest.mark.parametrize("name,text", NOTICE_NOT_EVIDENCE)
    def test_boilerplate_venue_and_ads_are_refused(self, name, text):
        assert notice_evidence(text) == [], name
        assert notice_verdict(text) == AMBIGUOUS

    def test_notice_path_never_declares_vacant(self):
        # silence in a legal notice proves nothing about the ground
        assert notice_verdict("Beginning at a iron pin, thence 180 poles.") \
            == AMBIGUOUS
        assert notice_verdict("") == AMBIGUOUS
        assert notice_verdict(None) == AMBIGUOUS

    def test_venue_sentence_does_not_void_a_real_conveyance(self):
        quotes = notice_evidence(dict(NOTICE_HAS)["Sullivan TN 542"])
        assert len(quotes) == 1
        assert "MOBILE HOME" in quotes[0].upper()
        assert "Circuit Court" not in quotes[0]

    def test_hyphen_split_ocr_still_reads(self):
        # notices OCR as "manufac- tured"; the scan re-joins the word
        assert notice_verdict(
            "Also conveyed herewith, a 2001 Jagu Jag Manufac- tured Home "
            "VIN No. GMHGA4190128142AB.") == HAS_BUILDING

    def test_the_zillow_path_is_what_over_promotes(self):
        """The reason a separate notice path exists."""
        name, text = NOTICE_NOT_EVIDENCE[0]
        assert existing_evidence(text)          # Zillow path: hits
        assert notice_evidence(text) == []      # notice path: refuses
