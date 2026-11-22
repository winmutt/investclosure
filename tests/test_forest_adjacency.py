"""Forest adjacency: description evidence + the GIS neighbour test.

The report depends on three measured facts, each pinned here:

* the owner strings NC OneMap actually publishes for public land (including
  the typos), and which ones are *not* public (tribal trust land, a golf
  club whose name contains "NATIONAL");
* description wording — adjacency asserted vs. proximity marketing vs.
  denial, judged sentence by sentence;
* the polygon-to-polygon gap, which is the honest adjacency measure because
  NC OneMap's ``IndexIntersects`` relation matches bounding boxes (a
  214,000-acre Great Smoky polygon "intersects" most of Jackson County).
"""
from __future__ import annotations

import pytest

from scraper import forest_adjacency as fa
from scraper import forest_report as fr


# ---------------------------------------------------------------------------
# Owner classification
# ---------------------------------------------------------------------------

class TestClassifyPublicOwner:
    @pytest.mark.parametrize("owner,kind", [
        ("USDA FOREST SERVICE ", "federal_forest"),
        ("US FOREST SERVICE", "federal_forest"),
        ("USFS", "federal_forest"),
        ("UNITED STATES FOREST SERVICE", "federal_forest"),
        ("GREAT SMOKY MTN NATI", "federal_park"),
        ("BLUE RIDGE PARKWAY", "federal_park"),
        ("THE UNITED STATES OF AMERICA", "federal"),
        ("UNITED STATES OF AMERICIA", "federal"),      # typo in the rolls
        ("STATE OF NORTH CAROLINA", "state"),
        ("N.C. STATE PARK-MEDOC MOUNTAIN", "state"),
        ("STATE OF NC", "state"),
    ])
    def test_public_owner_strings(self, owner, kind):
        assert fa.classify_public_owner(owner) == kind

    def test_county_owner_needs_the_county_name(self):
        assert fa.classify_public_owner("COUNTY OF HAYWOOD",
                                        "Haywood") == "county"
        assert fa.classify_public_owner("HAYWOOD COUNTY",
                                        "Haywood") == "county"
        # A private business on "County Line Road" is not a county holding.
        assert fa.classify_public_owner("COUNTY LINE PROPERTIES LLC",
                                        "Haywood") is None

    def test_private_and_tribal_are_not_public(self):
        assert fa.classify_public_owner("SEQUOYAH NATIONAL GOLF CLUB LLC",
                                        "Jackson") is None
        assert fa.classify_public_owner(
            "EASTERN BAND OF CHEROKEE INDIA UNITED STATES OF AMERICA,TRUS"
        ) is None
        assert fa.classify_public_owner("SMITH, JOHN") is None
        assert fa.classify_public_owner(None) is None


# ---------------------------------------------------------------------------
# Description evidence
# ---------------------------------------------------------------------------

class TestDescriptionEvidence:
    def test_asserted_adjacency_is_accepted(self):
        ev = fa.description_evidence(
            "Beautiful 12 acre tract that borders Pisgah National Forest.")
        assert ev.verdict == "adjacent"
        assert ev.kind == "federal_forest"
        assert ev.unit == "Pisgah National Forest"
        assert "borders" in ev.quote

    @pytest.mark.parametrize("text", [
        "Private inholding surrounded by Nantahala National Forest.",
        "Acreage adjoins the Great Smoky Mountains National Park boundary.",
        "This parcel abuts the state forest line.",
        "The property is adjacent to a 40,000 acre national forest tract.",
    ])
    def test_adjacency_phrasings(self, text):
        assert fa.description_evidence(text).verdict == "adjacent"

    @pytest.mark.parametrize("text", [
        "Only 10 minutes from Pisgah National Forest and downtown.",
        "Long-range mountain views, close to DuPont State Forest.",
        "National forest nearby.",
        "Just a short drive to the Nantahala National Forest.",
    ])
    def test_proximity_marketing_is_only_a_mention(self, text):
        assert fa.description_evidence(text).verdict == "mention"

    @pytest.mark.parametrize("text", [
        "This property is not adjacent to the national forest.",
        "The tract does not border Pisgah National Forest.",
        None,
        "Level building site with a well and septic permit.",
    ])
    def test_denials_and_unrelated_text_are_not_evidence(self, text):
        assert fa.description_evidence(text).verdict is None

    def test_forest_name_must_share_the_sentence_with_adjacency(self):
        # "Borders a bold creek" + a forest in another sentence is not
        # adjacency; the same sentence with distance wording is a mention.
        ev = fa.description_evidence(
            "The property borders a bold creek. National forest is nearby.")
        assert ev.verdict == "mention"
        ev = fa.description_evidence(
            "Borders a bold creek; national forest nearby.")
        assert ev.verdict == "mention"

    def test_state_forest_kind(self):
        ev = fa.description_evidence(
            "Borders DuPont State Forest on two sides.")
        assert ev.verdict == "adjacent"
        assert ev.kind == "state"

    def test_quote_is_bounded(self):
        ev = fa.description_evidence(
            "x " * 400 + "Borders Pisgah National Forest.")
        assert ev.verdict == "adjacent"
        assert len(ev.quote) <= 300

    @pytest.mark.parametrize("text", [
        # Live Zillow copy (Lake Hiwassee): the shoreline is surrounded by
        # forest, which says nothing about what this lot touches.
        "This TVA reservoir offers roughly 165 miles of shoreline, much of "
        "it surrounded by protected national forest land.",
        "Parts of the community border the national forest.",
        "The surrounding area is bordered by Nantahala National Forest.",
    ])
    def test_scenery_about_the_lake_or_region_is_not_adjacency(self, text):
        ev = fa.description_evidence(text)
        assert ev.verdict != "adjacent"

    def test_the_parcel_itself_can_still_be_surrounded(self):
        ev = fa.description_evidence(
            "Private inholding surrounded by Nantahala National Forest.")
        assert ev.verdict == "adjacent"

    @pytest.mark.parametrize("text", [
        "Borders U.S. Forest Service land.",
        "The property is partially fenced and bordered on one side by U.S. "
        "Forest Service land, offering unparalleled privacy.",
        "Almost 9 acres of UNRESTRICTED land bordering U.S. FOREST SERVICE "
        "land.",
    ])
    def test_us_abbreviation_does_not_split_the_sentence(self, text):
        # Splitting on "U.S." threw away the "bordered … by" half and lost
        # real adjacency evidence.
        assert fa.split_sentences(text.lower()) != [] 
        assert fa.description_evidence(text).verdict == "adjacent"

    @pytest.mark.parametrize("text", [
        # Live copy: the forest is named in the previous clause, and
        # "direct access to nature" is scenery, not contact.
        "Forest Service land, offering unparalleled privacy and direct "
        "access to nature.",
        "Forest Service land, you'll have added privacy and direct access "
        "to hundreds of acres of protected forest.",
    ])
    def test_weak_verb_must_be_followed_by_the_forest(self, text):
        # "direct access to nature" asserts nothing about a boundary.
        assert fa.description_evidence(text).verdict != "adjacent"

    def test_weak_verb_with_the_forest_after_it_counts(self):
        ev = fa.description_evidence(
            "Enjoy direct access to Pisgah National Forest from the back "
            "of the lot.")
        assert ev.verdict == "adjacent"

    def test_a_subdivision_being_surrounded_is_not_the_lot(self):
        ev = fa.description_evidence(
            "A cleared, graded building lot in Bailey Fields, a small gated "
            "community surrounded by national forest.")
        assert ev.verdict != "adjacent"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _square(x: float, y: float, size: float) -> list[list[tuple[float, float]]]:
    return [[(x, y), (x + size, y), (x + size, y + size), (x, y + size),
             (x, y)]]


class TestGeometry:
    def test_point_in_rings(self):
        ring = _square(-83.0, 35.0, 0.01)
        assert fa.point_in_rings(-82.995, 35.005, ring)
        assert not fa.point_in_rings(-83.02, 35.005, ring)
        assert not fa.point_in_rings(-82.995, 34.99, ring)

    def test_gap_between_touching_squares_is_zero(self):
        index = fa.ForestIndex()
        index.add(fa.PublicParcel("PUB-1", "US FOREST SERVICE",
                                  "federal_forest", 5_000, "099", "Jackson",
                                  rings=_square(-83.01, 35.00, 0.01)))
        subject = _square(-83.00, 35.00, 0.01)
        res = index.nearest(subject, lat=35.005, lon=-82.995)
        assert res["parcel"].parno == "PUB-1"
        assert res["gap_ft"] <= 5
        assert res["adjacent"] is True

    def test_gap_of_a_known_offset_is_measured(self):
        # 0.001 deg of latitude ≈ 364 ft; two squares 0.001 deg apart.
        index = fa.ForestIndex()
        index.add(fa.PublicParcel("PUB-1", "STATE OF NORTH CAROLINA",
                                  "state", 2_000, "099", "Jackson",
                                  rings=_square(-83.01, 35.002, 0.01)))
        subject = _square(-83.00, 35.00, 0.001)
        res = index.nearest(subject, lat=35.0, lon=-83.0)
        # Subject's top edge (35.001) is ~364 ft below the public parcel's
        # bottom edge (35.002) — the measured gap must still say "not
        # adjacent" at the 150 ft tolerance.
        assert 300 <= res["gap_ft"] <= 430
        assert res["adjacent"] is False

    def test_point_only_subject_is_marked(self):
        index = fa.ForestIndex()
        index.add(fa.PublicParcel("PUB-1", "USFS", "federal_forest", 5_000,
                                  "099", "Jackson",
                                  rings=_square(-83.001, 35.0, 0.001)))
        res = index.nearest(None, lat=35.0005, lon=-83.0005)
        assert res["geometry"] == "point"
        assert res["adjacent"] is True

    def test_empty_index_returns_none(self):
        assert fa.ForestIndex().nearest(None, lat=35.0, lon=-83.0) is None
        res = fa.ForestIndex().nearest(_square(-83.0, 35.0, 0.001),
                                       lat=35.0, lon=-83.0)
        assert res is None

    def test_nearest_prefers_the_closer_parcel(self):
        index = fa.ForestIndex()
        index.add(fa.PublicParcel("FAR", "US FOREST SERVICE",
                                  "federal_forest", 5_000, "099", "Jackson",
                                  rings=_square(-83.03, 35.0, 0.01)))
        index.add(fa.PublicParcel("NEAR", "STATE OF NORTH CAROLINA", "state",
                                  500, "099", "Jackson",
                                  rings=_square(-83.005, 35.0, 0.003)))
        res = index.nearest(_square(-83.0, 35.0, 0.001), lat=35.0, lon=-83.0)
        assert res["parcel"].parno == "NEAR"


# ---------------------------------------------------------------------------
# Index build filters (network mocked)
# ---------------------------------------------------------------------------

def _feature(parno, owner, acres, x, y, size=0.001):
    return {"attributes": {"parno": parno, "ownname": owner,
                           "gisacres": acres, "cntyname": "Jackson",
                           "cntyfips": "099"},
            "geometry": {"rings": [[[x, y], [x + size, y],
                                    [x + size, y + size], [x, y + size],
                                    [x, y]]]}}


class TestIndexBuild:
    def test_public_parcels_over_threshold_only(self, monkeypatch):
        feats = [
            _feature("A", "US FOREST SERVICE", 5000, -83.01, 35.0),
            _feature("B", "US FOREST SERVICE", 150, -83.02, 35.0),
            _feature("C", "SMITH, JOHN", 900, -83.03, 35.0),
            _feature("D", "EASTERN BAND OF CHEROKEE INDIA", 800, -83.04, 35.0),
        ]
        monkeypatch.setattr("scraper.nc_gis_lookup._nc1map_query",
                            lambda *a, **k: feats)
        got = fa.fetch_county_public_parcels("099", "Jackson")
        assert [p.parno for p in got] == ["A"]

    def test_split_pin_features_are_all_kept(self, monkeypatch):
        # Cherokee publishes 104 public records under only 8 distinct
        # parcel numbers; deduping on parno collapsed 156,000 acres of
        # national forest to a single polygon.
        feats = [_feature("SAME", "USDA FOREST SERVICE", 156309,
                          -83.0 - i * 0.01, 35.0) for i in range(4)]
        monkeypatch.setattr("scraper.nc_gis_lookup._nc1map_query",
                            lambda *a, **k: feats)
        got = fa.fetch_county_public_parcels("039", "Cherokee")
        assert len(got) == 4

    def test_coverage_note_names_data_gaps(self):
        coverage = {"021": {"county": "Buncombe", "parcels": 0},
                    "099": {"county": "Jackson", "parcels": 36}}
        notes = fa.coverage_note(coverage)
        assert len(notes) == 1
        assert "Buncombe" in notes[0]
        assert "Jackson" not in notes[0]


# ---------------------------------------------------------------------------
# Report thresholds + formatting
# ---------------------------------------------------------------------------

class TestReportScraper:
    def test_report_economics(self):
        scraper = fr.ForestZillowScraper()
        assert scraper.min_acres == 2.0
        assert scraper.max_price == 300_000
        assert scraper.fetch_details is False
        assert scraper.states == ["NC"]

    def test_county_filter_reaches_the_sweep(self, monkeypatch):
        seen: dict = {}

        def fake_sweep(self, state, counties, all_props):
            seen["counties"] = counties
            return False

        monkeypatch.setattr(fr.ZillowScraper, "_sweep_state", fake_sweep)
        scraper = fr.ForestZillowScraper(counties=["jackson", "macon"])
        scraper._sweep_state("NC", ["buncombe", "jackson", "macon"], [])
        assert seen["counties"] == ["jackson", "macon"]

    def test_all_nc_counties_when_unfiltered(self, monkeypatch):
        seen: dict = {}

        def fake_sweep(self, state, counties, all_props):
            seen["counties"] = counties
            return False

        monkeypatch.setattr(fr.ZillowScraper, "_sweep_state", fake_sweep)
        scraper = fr.ForestZillowScraper()
        counties = ["buncombe", "jackson"]
        scraper._sweep_state("NC", counties, [])
        assert seen["counties"] == counties

    def test_scrape_uses_the_15_county_mountain_set(self, monkeypatch):
        # QUALIFYING_COUNTIES["NC"] holds 17 names (the foreclosure set);
        # the report was requested for the in-scope mountain counties only.
        from scraper.config import NC_MOUNTAIN_COUNTIES

        seen: dict = {}

        def fake_sweep(self, state, counties, all_props):
            seen["counties"] = list(counties)
            return False

        monkeypatch.setattr(fr.ZillowScraper, "_sweep_state", fake_sweep)
        fr.ForestZillowScraper().scrape()
        assert len(NC_MOUNTAIN_COUNTIES) == 15
        assert seen["counties"] == list(NC_MOUNTAIN_COUNTIES)


class TestScope:
    def test_only_the_15_mountain_counties_are_in_scope(self):
        assert fr._in_scope({"county": "Jackson", "state": "NC"})
        assert fr._in_scope({"county": "Mcdowell", "state": "NC"})
        # Zillow's search leaks neighbours and the town/ZIP cross-reference
        # re-hangs some rows on these two, which are outside the pruned scope.
        assert not fr._in_scope({"county": "Rutherford", "state": "NC"})
        assert not fr._in_scope({"county": "Cleveland", "state": "NC"})
        assert not fr._in_scope({"county": "Jackson", "state": "GA"})
        assert not fr._in_scope({"county": None, "state": "NC"})

    def test_candidates_filter_reports_what_it_dropped(self, monkeypatch):
        rows = [{"id": 1, "county": "Jackson", "state": "NC"},
                {"id": 2, "county": "Rutherford", "state": "NC"},
                {"id": 3, "county": "Polk", "state": "NC"}]
        monkeypatch.setattr(fr, "load_done",
                            lambda *a, **k: {r["id"]: r for r in rows})
        inside, dropped = fr._candidates()
        assert [r["id"] for r in inside] == [1, 3]
        assert dropped == 1


class TestResolveSubjectParcel:
    def _hit_feature(self, parno, acres, x, y, size=0.002):
        return _feature(parno, "SMITH, JOHN", acres, x, y, size)

    def _patch(self, monkeypatch, feats):
        monkeypatch.setattr("scraper.nc_gis_lookup._nc1map_query",
                            lambda *a, **k: feats)

    def test_coordinate_inside_parcel_resolves_its_polygon(self, monkeypatch):
        self._patch(monkeypatch,
                    [self._hit_feature("P1", 10.0, -83.002, 34.999)])
        got = fa.resolve_subject_parcel(35.0, -83.001, county="Jackson",
                                        expected_acres=10.0)
        assert got["parno"] == "P1"
        assert got["method"] == "point"

    def test_pin_in_a_road_gap_never_borrows_a_neighbours_polygon(
            self, monkeypatch):
        # 3 acres is not this 40-acre listing, so using that polygon would
        # attribute the neighbour's forest line to the listing.  The pin is
        # still a location, so the GIS test may measure from it — but the
        # rejected polygon must be recorded, never used.
        self._patch(monkeypatch,
                    [self._hit_feature("NEIGHBOUR", 3.0, -83.002, 34.999)])
        got = fa.resolve_subject_parcel(35.01, -83.001, county="Jackson",
                                        expected_acres=40.0)
        assert got["method"] == "point-only"
        assert got["rings"] is None
        assert got["acreage_mismatch"][0]["parno"] == "NEIGHBOUR"

    def test_pin_in_a_road_gap_with_matching_acres_is_point_near(
            self, monkeypatch):
        self._patch(monkeypatch,
                    [self._hit_feature("OWN", 40.0, -83.002, 34.999)])
        got = fa.resolve_subject_parcel(35.01, -83.001, county="Jackson",
                                        expected_acres=40.0)
        assert got["parno"] == "OWN"
        assert got["method"] == "point-near"

    def test_inside_a_public_polygon_far_too_big_is_point_only(
            self, monkeypatch):
        # Live QA: a 2.0 ac listing's pin sat inside the 214,157 ac Great
        # Smoky polygon, and that polygon became the "subject".
        self._patch(monkeypatch,
                    [self._hit_feature("GSMP", 214_157.0, -83.5, 35.4)])
        got = fa.resolve_subject_parcel(35.4, -83.5, county="Swain",
                                        expected_acres=2.0)
        assert got["method"] == "point-only"
        assert got["rings"] is None

    def test_address_hit_with_wrong_acreage_is_not_used(self, monkeypatch):
        # Live QA: a 10.66 ac listing resolved through its address to a
        # 4.53 ac parcel (a different owner's lot on the same road).
        feats = [self._hit_feature("WRONG", 4.53, -83.002, 34.999)]
        monkeypatch.setattr("scraper.nc_gis_lookup._nc1map_query",
                            lambda *a, **k: feats)
        monkeypatch.setattr(
            "scraper.nc_gis_lookup.search_address_in_nc1map",
            lambda *a, **k: {"parno": "WRONG"})
        got = fa.resolve_subject_parcel(35.0, -83.0, address="3215 Grape Crk",
                                        county="Cherokee",
                                        expected_acres=10.66)
        assert got["method"] == "point-only"
        assert any(r["parno"] == "WRONG" for r in got["acreage_mismatch"])

    def test_address_hit_with_matching_acreage_is_used(self, monkeypatch):
        feats = [self._hit_feature("RIGHT", 10.5, -83.002, 34.999)]
        monkeypatch.setattr("scraper.nc_gis_lookup._nc1map_query",
                            lambda *a, **k: feats)
        monkeypatch.setattr(
            "scraper.nc_gis_lookup.search_owner_parcels_in_nc1map",
            lambda *a, **k: None)
        monkeypatch.setattr(
            "scraper.nc_gis_lookup.search_address_in_nc1map",
            lambda *a, **k: {"parno": "RIGHT"})
        got = fa.resolve_subject_parcel(None, None, address="3215 Grape Crk",
                                        county="Cherokee",
                                        expected_acres=10.66)
        assert got["parno"] == "RIGHT"
        assert got["method"] == "address"

    def test_multi_parcel_tract_is_merged_by_ownership_group(
            self, monkeypatch):
        # 26.02 ac listing = 23.0 ac + 3.02 ac, same owner (the #2739 case):
        # both polygons make up the subject, so the forest line is measured
        # against the whole tract.
        geoms = {"111": self._hit_feature("111", 23.0, -83.002, 34.999),
                 "222": self._hit_feature("222", 3.02, -83.010, 34.995)}
        monkeypatch.setattr(
            "scraper.nc_gis_lookup._nc1map_query",
            lambda params, **k: (
                [geoms[params["where"].split("'")[1]]]
                if params.get("where", "").startswith("parno=") else []))
        monkeypatch.setattr(
            "scraper.nc_gis_lookup.search_owner_parcels_in_nc1map",
            lambda *a, **k: {"parno": "111", "acres": 26.02,
                             "parcels": [{"parno": "111", "acres": 23.0},
                                         {"parno": "222", "acres": 3.02}]})
        got = fa.resolve_subject_parcel(35.01, -83.03, address="0 Leon Barron",
                                        county="Jackson",
                                        expected_acres=26.02)
        assert got["method"] == "parcel-group"
        assert got["acres"] == 26.02
        assert len(got["rings"]) == 2
        assert [p["parno"] for p in got["parcels"]] == ["111", "222"]

    def test_no_coordinates_and_no_parcel_returns_none(self):
        assert fa.resolve_subject_parcel(None, None,
                                         county="Jackson") is None


class TestSubjectPlausibility:
    def test_point_only_is_plausible(self):
        # No polygon, so nothing about it can belong to a neighbour.
        assert fa.subject_is_plausible({"method": "point-only"}, 5.0)

    def test_missing_subject_is_not(self):
        assert not fa.subject_is_plausible(None, 5.0)

    def test_a_grossly_wrong_polygon_is_not(self):
        # Live QA: 2.0 ac listing bound to the 214,157 ac Great Smoky polygon.
        assert not fa.subject_is_plausible(
            {"method": "point", "acres": 214_157.0}, 2.0)

    def test_a_matching_polygon_is(self):
        assert fa.subject_is_plausible({"method": "address",
                                        "acres": 10.5}, 10.66)

    def test_an_unknown_method_is_not(self):
        assert not fa.subject_is_plausible({"method": "guess", "acres": 5.0},
                                           5.0)


class TestAdjacencyTodo:
    def _cand(self, zpid, acres=None):
        return {"id": zpid, "county": "Jackson", "state": "NC",
                "acres": acres}

    def test_unjudged_candidates_are_pending(self):
        got = fr.adjacency_todo([self._cand(1)], {}, {})
        assert [c["id"] for c in got] == [1]

    def test_judged_candidate_is_skipped_when_description_is_unchanged(self):
        details = {1: {"description": "abc"}}
        done = {1: {"verdict": "no", "desc_len": 3, "subject_method": "none"}}
        assert fr.adjacency_todo([self._cand(1)], details, done) == []

    def test_verdict_recorded_before_the_description_is_rejudged(self):
        # GIS-only verdicts carry desc_len 0; the description arrived later.
        details = {1: {"description": "borders Pisgah National Forest"}}
        done = {1: {"verdict": "no", "desc_len": 0}}
        assert len(fr.adjacency_todo([self._cand(1)], details, done)) == 1

    def test_missing_desc_len_on_old_records_is_rejudged(self):
        details = {1: {"description": "abc"}}
        done = {1: {"verdict": "no"}}
        assert len(fr.adjacency_todo([self._cand(1)], details, done)) == 1

    def test_legacy_verdict_with_implausible_subject_is_rejudged(self):
        # Checkpoints written before the acreage guard bound a 2 ac listing
        # to the 214,157 ac Great Smoky polygon.
        details = {1: {"description": "abc"}}
        done = {1: {"verdict": "adjacent", "desc_len": 3,
                    "subject": {"method": "point", "acres": 214_157.0}}}
        assert len(fr.adjacency_todo([self._cand(1, acres=2.0)],
                                     details, done)) == 1

    def test_legacy_verdict_with_plausible_subject_is_kept(self):
        details = {1: {"description": "abc"}}
        done = {1: {"verdict": "adjacent", "desc_len": 3,
                    "subject": {"method": "address", "acres": 10.5}}}
        assert fr.adjacency_todo([self._cand(1, acres=10.66)],
                                 details, done) == []

    def test_current_point_only_verdict_is_stable(self):
        details = {1: {"description": "abc"}}
        done = {1: {"verdict": "no", "desc_len": 3, "subject_method":
                    "point-only", "subject": {"method": "point-only"}}}
        assert fr.adjacency_todo([self._cand(1)], details, done) == []


class TestAdjacencyGuard:
    def test_empty_index_aborts_rather_than_failing_everyone(
            self, monkeypatch):
        # A public-parcel outage must never mean "nobody is adjacent".
        monkeypatch.setattr(fr, "_ensure_tmp", lambda: None)
        monkeypatch.setattr(
            fr, "_candidates",
            lambda: ([{"id": 7, "county": "Jackson", "state": "NC",
                       "url": "http://x"}], 0))
        monkeypatch.setattr(fr, "load_done", lambda *a, **k: {})
        monkeypatch.setattr(fr, "build_index",
                            lambda **k: (fa.ForestIndex(), {}))

        class Args:
            refresh_index = False
            tolerance_ft = 150.0

        with pytest.raises(RuntimeError):
            fr.stage_adjacency(Args())


class TestGisAdjacencyNeedsAParcel:
    def _why(self, verdict=None):
        return fa.DescriptionEvidence(verdict=verdict)

    def test_a_polygon_subject_makes_the_gis_claim(self):
        verdict, source, withheld = fr.judge_verdict(
            self._why(), {"gap_ft": 12.0},
            {"method": "point", "acres": 5.0}, 150.0)
        assert (verdict, source, withheld) == ("adjacent", "gis", False)

    def test_a_pin_only_subject_cannot_make_the_gis_claim(self):
        # Live QA: the pin sat 9 ft from the Great Smoky polygon, but the
        # roll had no acreage-plausible parcel for this listing.
        verdict, source, withheld = fr.judge_verdict(
            self._why(), {"gap_ft": 9.0},
            {"method": "point-only", "acres": None}, 150.0)
        assert (verdict, source, withheld) == ("no", None, True)

    def test_an_unresolved_subject_cannot_make_the_gis_claim(self):
        verdict, source, withheld = fr.judge_verdict(
            self._why(), {"gap_ft": 0.0}, None, 150.0)
        assert (verdict, source, withheld) == ("no", None, True)

    def test_a_description_claim_needs_no_polygon(self):
        verdict, source, withheld = fr.judge_verdict(
            self._why("adjacent"), None, None, 150.0)
        assert (verdict, source, withheld) == ("adjacent", "description", False)

    def test_outside_the_tolerance_is_no(self):
        verdict, source, withheld = fr.judge_verdict(
            self._why(), {"gap_ft": 400.0},
            {"method": "point", "acres": 5.0}, 150.0)
        assert (verdict, source, withheld) == ("no", None, False)

    def test_a_mention_is_recorded_when_nothing_is_close(self):
        verdict, source, withheld = fr.judge_verdict(
            self._why("mention"), None, None, 150.0)
        assert (verdict, source, withheld) == ("mention", None, False)


class TestPostingDedupe:
    def _row(self, address=None, acres=2.0, price=100_000, parno=None,
             county="Cherokee"):
        return {"address": address, "acres": acres, "price": price,
                "county": county, "subject": {"parno": parno} if parno else None}

    def test_the_same_parcel_reposted_under_a_new_zpid_collapses(self):
        rows = [self._row("247 Rice Ln, Marble, NC 28905", 2.01, 80_000,
                          parno="553300294609000"),
                self._row("247 Rice Lane LOT 4, Murphy, NC 28906", 2.01, 80_000,
                          parno="553300294609000")]
        kept, dropped = fr.dedupe_adjacent(rows)
        assert dropped == 1
        assert kept[0]["address"].startswith("247 Rice Ln")

    def test_two_different_parcels_on_one_street_stay(self):
        rows = [self._row("10 Roaming Deer Rd", 2.94, 39_900, parno="A"),
                self._row("12 Roaming Deer Rd", 2.94, 39_900, parno="B")]
        kept, dropped = fr.dedupe_adjacent(rows)
        assert dropped == 0 and len(kept) == 2

    def test_one_parcel_with_a_different_price_stays(self):
        rows = [self._row("1 Ridge Rd", 5.0, 100_000, parno="A"),
                self._row("1 Ridge Rd", 5.0, 120_000, parno="A")]
        kept, dropped = fr.dedupe_adjacent(rows)
        assert dropped == 0 and len(kept) == 2

    def test_without_a_parcel_the_street_suffix_is_normalised(self):
        a = self._row("247 Rice Ln, Marble, NC 28905")
        b = self._row("247 Rice Lane LOT 4, Murphy, NC 28906")
        assert fr.posting_key(a) == fr.posting_key(b)
        kept, dropped = fr.dedupe_adjacent([a, b])
        assert dropped == 1

    def test_unknown_acres_do_not_collide_on_street_alone(self):
        a = self._row("9 Creek Rd", None, 50_000)
        b = self._row("9 Creek Rd", None, 50_000)
        # Same street, same price, two postings with no acreage → a duplicate
        # is still the honest reading (0 == 0 acres).
        kept, dropped = fr.dedupe_adjacent([a, b])
        assert dropped == 1


class TestNearMisses:
    def _row(self, gap, verdict="no"):
        return {"verdict": verdict, "nearest_public": {"gap_ft": gap}}

    def test_band_is_tolerance_to_ceiling(self):
        rows = [self._row(0.0, "adjacent"), self._row(140),
                self._row(200), self._row(499), self._row(900),
                {"verdict": "no", "nearest_public": None}]
        got = fr.near_misses(rows, 150.0)
        assert [r["nearest_public"]["gap_ft"] for r in got] == [200, 499]

    def test_sorted_by_gap(self):
        got = fr.near_misses([self._row(400), self._row(175)], 150.0)
        assert [r["nearest_public"]["gap_ft"] for r in got] == [175, 400]


class TestFormatting:
    @pytest.mark.parametrize("value,digits,expected", [
        (1700, 0, "1,700"),
        (0, 0, "0"),
        (2.5, 2, "2.5"),
        (3.0, 2, "3"),
        (None, 0, "—"),
        (12345.67, 2, "12,345.67"),
    ])
    def test_num(self, value, digits, expected):
        assert fr._num(value, digits) == expected

    def test_gis_evidence_carries_gap_and_owner(self):
        row = {"source": "gis",
               "nearest_public": {"owner": "US FOREST SERVICE",
                                  "acres": 689.0, "gap_ft": 0.0}}
        text = fr._evidence(row)
        assert "US FOREST SERVICE" in text
        assert "689" in text
        assert "0 ft away" in text

    def test_description_evidence_is_quoted(self):
        row = {"source": "description",
               "nearest_public": None,
               "description_evidence": {
                   "verdict": "adjacent", "kind": "federal_forest",
                   "unit": "Pisgah National Forest",
                   "quote": "borders Pisgah National Forest"}}
        text = fr._evidence(row)
        assert text.startswith("“")
        assert "borders Pisgah" in text

    @pytest.mark.parametrize("kind,expected", [
        ("federal_forest", "national forest (unnamed in listing)"),
        ("federal_park", "national park (unnamed in listing)"),
        ("state", "state land (unnamed in listing)"),
        ("county", "county forest (unnamed in listing)"),
        (None, "public land (unnamed in listing)"),
    ])
    def test_kind_label_never_invents_an_owner(self, kind, expected):
        assert fr._kind_label(kind) == expected
