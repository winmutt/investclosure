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
    description_verdict, existing_evidence, insights_verdict, needs_images)

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
