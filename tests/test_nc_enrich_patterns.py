"""NC OneMap enrichment: query patterns, the acreage fields, the backlog.

Three measured defects kept NC rows NULL even though OneMap had the answer:

* ``LIKE`` is CASE-SENSITIVE on that service and the rolls store situs
  addresses in UPPER CASE — ``siteadd LIKE '%pigeon%creek%'`` returns 0 rows
  while ``'%PIGEON%CREEK%'`` returns 3. ``street_canon`` hands back lowercase
  abbreviations, so the last-resort tier had never matched anything (#5658).
* the rolls pad their spacing AND abbreviate at once ("177   PIGEON CREEK
  TRL"), so a ``%``-join has to be built from each spelling variant.
* Buncombe publishes ``gisacres = 0.0`` on every parcel and keeps acreage in
  ``recareano``, which the address query never requested — a correct hit was
  discarded by ``_clean_features`` (#6050, 81 Eastview Cir, 0.46 ac).

Plus the two plumbing bugs those probes exposed: a PIN stored without the
dashes the county keeps (#6052), and a post-run pass scoped to ``id > min_id``
so a lookup that failed once was never retried after the reason was fixed
(#6084, #5657).
"""

import re

import pytest

import re

from scraper import nc_gis_lookup as N
from scraper.db import _ensure_db, insert_property


# ---------------------------------------------------------------------------
# LIKE patterns
# ---------------------------------------------------------------------------

class TestLikePatterns:
    def test_canon_tier_is_upper_cased(self):
        # OneMap LIKE is case-sensitive; canon_tokens() returns lowercase.
        patterns = N._like_patterns("PIGEON CREEK")
        assert "PIGEON%CREEK" in patterns
        assert all(p == p.upper() for p in patterns)

    def test_gap_form_is_built_for_every_variant(self):
        patterns = N._like_patterns("PIGEON CREEK TRAIL", "177")
        assert "177%PIGEON%CRK%TRAIL" in patterns   # abbreviated AND padded
        assert patterns.index("177 PIGEON CREEK TRAIL") < \
            patterns.index("177%PIGEON%CRK%TRAIL")

    def test_house_number_anchored_loosest_tier(self):
        # Swain stores "177   PIGEON CREEK TRL": only a % from the house
        # number out can reach it (the street type is abbreviated, which
        # street_canon does not map for NC's TRAIL).
        patterns = N._like_patterns("PIGEON CREEK TRAIL", "177")
        assert "177%PIGEON%CREEK" in patterns
        assert patterns[-1] == "PIGEON%CREEK"       # unanchored stays last

    def test_anchored_patterns_come_first(self):
        patterns = N._like_patterns("EASTVIEW CIR", "81")
        assert patterns[0] == "81 EASTVIEW CIR"
        assert patterns.index("81 EASTVIEW CIR") < patterns.index("EASTVIEW CIR")

    def test_no_house_number_yields_no_anchored_tier(self):
        assert all("%81" not in p and "81 " not in p
                   for p in N._like_patterns("EASTVIEW CIR"))

    def test_empty_input(self):
        assert N._like_patterns("") == []
        assert N._like_patterns("", "177") == []


class TestAddressSearchUsesTheRightFields:
    """#6050: Buncombe's hit was thrown away because recareano wasn't asked."""

    def _feature(self, gisacres, recareano):
        return {"attributes": {
            "parno": "961891714900000", "altparno": "", "nparno": "",
            "cntyname": "Buncombe", "cntyfips": "021",
            "gisacres": gisacres, "recareano": recareano,
            "siteadd": "81 EASTVIEW  CIR", "ownname": "LENTZ INVESTMENTS LLC",
        }, "geometry": None}

    def _run(self, monkeypatch, feature):
        calls = []

        def spy(post_data, timeout=15):
            calls.append(post_data)
            # The first pattern misses; the gap form finds the parcel.
            where = post_data["where"]
            return [feature] if "81%EASTVIEW%CIR" in where else []

        monkeypatch.setattr(N, "_nc1map_query", spy)
        result = N.search_address_in_nc1map(
            "81 Eastview Circle, Asheville, NC 28806", "Buncombe")
        return calls, result

    def test_query_requests_recareano(self, monkeypatch):
        calls, _ = self._run(monkeypatch, self._feature(0.0, 0.46))
        assert calls, "no OneMap query was issued"
        for call in calls:
            assert "recareano" in call["outFields"]

    def test_zero_gisacres_falls_back_to_recareano(self, monkeypatch):
        _, result = self._run(monkeypatch, self._feature(0.0, 0.46))
        assert result is not None
        assert result["acres"] == 0.46
        assert result["owner_name"] == "LENTZ INVESTMENTS LLC"

    def test_wrong_house_is_still_rejected(self, monkeypatch):
        feature = self._feature(0.0, 0.46)
        feature["attributes"]["siteadd"] = "89 EASTVIEW  CIR"

        def spy(post_data, timeout=15):
            return [feature] if "81%EASTVIEW%CIR" in post_data["where"] else []

        monkeypatch.setattr(N, "_nc1map_query", spy)
        assert N.search_address_in_nc1map(
            "81 Eastview Circle, Asheville, NC 28806", "Buncombe") is None


class TestQueryRetry:
    """A slow OneMap response used to be indistinguishable from no match."""

    class _Resp:
        def __init__(self, payload, status=200):
            self._payload, self.status_code = payload, status

        def json(self):
            return self._payload

    def test_transport_error_is_retried_once(self, monkeypatch):
        from scraper import nconemap_net
        calls = []

        def post(url, data=None, timeout=None, **kw):
            calls.append(timeout)
            if len(calls) == 1:
                raise RuntimeError("timed out")
            return self._Resp({"features": [{"attributes": {"parno": "1"}}]})

        monkeypatch.setattr(nconemap_net, "post", post)
        feats = N._nc1map_query({"where": "1=1"}, timeout=5)
        assert [f["attributes"]["parno"] for f in feats] == ["1"]
        assert len(calls) == 2 and calls[1] >= 10

    def test_service_error_is_not_retried(self, monkeypatch):
        from scraper import nconemap_net
        calls = []

        def post(url, data=None, timeout=None, **kw):
            calls.append(timeout)
            return self._Resp({"error": {"message": "Invalid field"}})

        monkeypatch.setattr(nconemap_net, "post", post)
        assert N._nc1map_query({"where": "1=1"}, timeout=5) is None
        assert len(calls) == 1

    def test_empty_result_is_empty_not_retried(self, monkeypatch):
        from scraper import nconemap_net
        calls = []

        def post(url, data=None, timeout=None, **kw):
            calls.append(timeout)
            return self._Resp({"features": []})

        monkeypatch.setattr(nconemap_net, "post", post)
        assert N._nc1map_query({"where": "1=1"}, timeout=5) == []
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# the backlog
# ---------------------------------------------------------------------------

def _insert(conn, source, county, state, address=None, parcel=None,
            status="active"):
    _kind, row = insert_property(
        conn, source=source,
        source_listing_id=f"{source}:{county}:{parcel or address or 'x'}",
        url="http://x", address=address, city=None, county=county,
        state=state, zip_code=None, latitude=None, longitude=None,
        price_cents=0, acres=None, parcel_number=parcel)
    if status != "active":
        conn.execute("UPDATE properties SET status=? WHERE id=?",
                     (status, row["id"]))
        conn.commit()
    return row["id"]


@pytest.fixture
def nc_db(tmp_path, monkeypatch):
    from scraper.config import config
    db_path = tmp_path / "nc.db"
    _ensure_db(db_path).close()
    monkeypatch.setattr(config, "db_path", db_path)
    return db_path


@pytest.fixture
def no_lookup(monkeypatch):
    """Every OneMap lookup misses; returns the text each lookup was given."""
    seen = []

    def _miss(*args, **kwargs):
        seen.append(" ".join(str(a) for a in args if isinstance(a, str)))
        return None

    monkeypatch.setattr(N, "county_specific_lookup", _miss)
    monkeypatch.setattr(N, "_lookup_parcel_multi", _miss)
    monkeypatch.setattr(N, "search_address_in_nc1map", _miss)
    return seen


class TestBacklog:
    # ``min_id`` is the highest id from BEFORE a run inserted anything, so
    # this run's rows are ``id > min_id`` and the backlog is ``id <= min_id``.
    def test_older_nc_rows_are_retried(self, nc_db, no_lookup):
        conn = _ensure_db(nc_db)
        old = _insert(conn, "nc_publicnotice", "Macon", "NC",
                      address="261 Trimont Trail")
        _insert(conn, "nc_publicnotice", "Jackson", "NC",
                address="38 Chase Ln")
        conn.close()
        stats = N.enrich_properties(min_id=old, backlog=10)
        assert stats["backlog"] == 1
        assert any("Trimont" in s for s in no_lookup), \
            "the older row was never looked up"
        assert any("Chase" in s for s in no_lookup)

    def test_without_a_backlog_only_the_window_runs(self, nc_db, no_lookup):
        conn = _ensure_db(nc_db)
        old = _insert(conn, "nc_publicnotice", "Macon", "NC",
                      address="261 Trimont Trail")
        _insert(conn, "nc_publicnotice", "Jackson", "NC",
                address="38 Chase Ln")
        conn.close()
        stats = N.enrich_properties(min_id=old)
        assert stats["backlog"] == 0
        assert not any("Trimont" in s for s in no_lookup)

    def test_backlog_is_scoped_to_nc(self, nc_db, no_lookup):
        """OneMap covers no other state; a TN row must not eat the budget."""
        conn = _ensure_db(nc_db)
        tn = _insert(conn, "brockandscott", "Hamilton", "TN",
                     address="2912 N Chamberlain Ave")
        _insert(conn, "nc_publicnotice", "Jackson", "NC",
                address="38 Chase Ln")
        conn.close()
        stats = N.enrich_properties(min_id=tn, backlog=10)
        assert stats["backlog"] == 0
        assert not any("Chamberlain" in s for s in no_lookup)

    def test_backlog_oldest_first_and_bounded(self, nc_db, no_lookup):
        conn = _ensure_db(nc_db)
        last_old = None
        for i in range(4):
            last_old = _insert(conn, "nc_publicnotice", "Macon", "NC",
                               address=f"{100 + i} Trimont Trail")
        _insert(conn, "nc_publicnotice", "Jackson", "NC",
                address="38 Chase Ln")
        conn.close()
        stats = N.enrich_properties(min_id=last_old, backlog=2)
        assert stats["backlog"] == 2
        # Each row is looked up twice (county service, then OneMap address),
        # so compare the ORDER of distinct house numbers, not the call list.
        tried = list(dict.fromkeys(
            re.search(r"\b10\d\b", s).group(0) for s in no_lookup
            if "Trimont" in s))
        assert tried == ["100", "101"]

    def test_archived_rows_are_not_retried(self, nc_db, no_lookup):
        conn = _ensure_db(nc_db)
        old = _insert(conn, "nc_publicnotice", "Macon", "NC",
                      address="261 Trimont Trail", status="archived")
        _insert(conn, "nc_publicnotice", "Jackson", "NC",
                address="38 Chase Ln")
        conn.close()
        assert N.enrich_properties(min_id=old, backlog=10)["backlog"] == 0
