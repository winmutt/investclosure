"""Forest-adjacency report: Zillow land next to government forest in NC.

A *report*, not an inventory import.  It sweeps the 15 in-scope NC mountain
counties with this report's own economics — **≥ 2 acres, ≤ $300,000,
ground ≥ 1,700 ft** — and keeps only listings that sit next to national,
state or county forest land whose public parcel exceeds **200 acres**.

Evidence order (per the product request):

1. the listing description (:func:`forest_adjacency.description_evidence`),
2. failing that, the GIS neighbour test (:class:`forest_adjacency.
   ForestIndex`) — NC OneMap carries the federal/state holdings themselves,
   so adjacency is measured against their real polygons.

Nothing is written to the ``properties`` table: the report is produced in
``reports/forest_adjacency.md`` (plus a JSON dump), so a sweep at $300k /
2 acres cannot leak rows into the dashboard's tighter inventory.

Every stage checkpoints to ``scraper/tmp/forest_report/`` and resumes:

    python3 -m scraper.forest_report --stage sweep            # county pages
    python3 -m scraper.forest_report --stage details          # descriptions
    python3 -m scraper.forest_report --stage adjacency        # parcel + forest
    python3 -m scraper.forest_report --stage elevation
    python3 -m scraper.forest_report --stage report
    python3 -m scraper.forest_report                          # all stages

Useful flags: ``--county jackson`` (one county), ``--limit-details 20``,
``--refresh-index`` (re-fetch the public-parcel index), ``--min-elevation``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import time
from typing import Optional

from .config import NC_MOUNTAIN_COUNTIES
from .elevation import elevations
from .forest_adjacency import (
    ADJACENCY_TOLERANCE_FT, MIN_PUBLIC_ACRES, POLYGON_METHODS,
    build_index, coverage_note, description_evidence, resolve_subject_parcel,
    subject_is_plausible,
)
from .jsonl import append_record, load_done
from .nc_gis_lookup import street_only
from .zillow import ZillowScraper, county_display

logger = logging.getLogger(__name__)

TMP_DIR = os.path.join(os.path.dirname(__file__), "tmp", "forest_report")
CANDIDATES = os.path.join(TMP_DIR, "candidates.jsonl")
DETAILS = os.path.join(TMP_DIR, "details.jsonl")
ELEV_FILE = os.path.join(TMP_DIR, "elevations.jsonl")
ADJ_FILE = os.path.join(TMP_DIR, "adjacency.jsonl")
SWEPT = os.path.join(TMP_DIR, "swept.json")
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "reports")
REPORT_MD = os.path.join(REPORT_DIR, "forest_adjacency.md")
REPORT_JSON = os.path.join(REPORT_DIR, "forest_adjacency.json")

# The report's economics, separate from the dashboard's inventory rules
# (config.MIN_ACRES 1.1 / zillow 5 ac, $250k).
REPORT_MIN_ACRES = 2.0
REPORT_MAX_PRICE = 300_000
REPORT_MAX_ACRES = 1_000.0
REPORT_MIN_ELEVATION = 1_700.0

COUNTRY_STATE = "NC"
NC_SCOPE = {c.lower() for c in NC_MOUNTAIN_COUNTIES}


def _in_scope(cand: dict) -> bool:
    """Is this candidate in one of the 15 in-scope NC mountain counties?

    Zillow's own search leaks neighbours (a Polk County page can return a
    Forest City listing) and the town/ZIP cross-reference re-hangs some rows
    on Rutherford or Cleveland, so the resolved county — not the searched
    one — decides scope here.
    """
    return ((cand.get("state") or "").upper() == COUNTRY_STATE
            and (cand.get("county") or "").lower() in NC_SCOPE)


def _candidates() -> tuple[list[dict], int]:
    """Every checkpointed candidate, and how many are out of scope."""
    all_rows = list(load_done(CANDIDATES).values())
    inside = [c for c in all_rows if _in_scope(c)]
    return inside, len(all_rows) - len(inside)


class ForestZillowScraper(ZillowScraper):
    """Zillow county sweep with the report's thresholds and NC scope only."""

    def __init__(self, min_acres: float = REPORT_MIN_ACRES,
                 max_price: int = REPORT_MAX_PRICE,
                 max_acres: float = REPORT_MAX_ACRES,
                 states: tuple[str, ...] = (COUNTRY_STATE,),
                 counties: Optional[list[str]] = None):
        super().__init__(fetch_details=False, states=list(states),
                         detail_skip=set(), detail_limit=0)
        self.min_acres = float(min_acres)
        self.max_price = int(max_price)
        self.max_acres = float(max_acres)
        self.fetch_details = False
        self.only_counties = ({c.lower() for c in counties}
                              if counties else None)

    def _sweep_state(self, state: str, counties: list[str],
                     all_props: list) -> bool:
        todo = ([c for c in counties if c.lower() in self.only_counties]
                if self.only_counties else counties)
        if not todo:
            logger.info("── %s: no requested counties in scope", state)
            return False
        return super()._sweep_state(state, todo, all_props)

    def scrape(self) -> list:
        """NC mountain counties only — never QUALIFYING_COUNTIES' extra 2.

        ``QUALIFYING_COUNTIES["NC"]`` carries 17 names (the foreclosure set),
        two of which are outside the pruned mountain scope.  The report was
        built to sweep exactly ``config.NC_MOUNTAIN_COUNTIES``.
        """
        all_props: list = []
        counties = [c for c in NC_MOUNTAIN_COUNTIES
                    if not self.only_counties
                    or c.lower() in self.only_counties]
        self._sweep_state(COUNTRY_STATE, counties, all_props)
        return self._dedupe(all_props)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _ensure_tmp() -> None:
    os.makedirs(TMP_DIR, exist_ok=True)


def _load_swept() -> dict:
    try:
        with open(SWEPT, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_swept(state: dict) -> None:
    _ensure_tmp()
    tmp = SWEPT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, SWEPT)


def stage_sweep(args) -> int:
    """Walk every page of the NC county searches, county by county.

    One county at a time, saved as soon as it finishes: Zillow walls long
    sessions and a crashed browser once cost a whole Buncombe walk, so the
    sweep must never hold 15 counties of work in memory.
    """
    scraper = ForestZillowScraper(min_acres=args.min_acres,
                                  max_price=args.max_price)
    wanted = {c.lower() for c in (args.county or [])}
    counties = [c for c in NC_MOUNTAIN_COUNTIES
                if not wanted or c.lower() in wanted]
    state = _load_swept()
    existing = load_done(CANDIDATES)
    added = 0
    now = int(time.time())
    consecutive = 0
    logger.info("forest report: sweeping %d of %d NC counties",
                sum(1 for c in counties if c.lower() not in state
                    or args.resweep), len(counties))
    for county in counties:
        key = county.lower()
        if key in state and not args.resweep and not wanted:
            logger.info("  %s: already swept (%d candidates) — skipping",
                        county_display(county), state[key].get("qualifying"))
            continue
        props, challenged, returned, pages = scraper._scrape_county(county,
                                                                    "NC")
        new = 0
        out_of_scope = 0
        for prop in props:
            zpid = str(prop.get("source_listing_id") or "")
            if not zpid.isdigit() or int(zpid) in existing:
                continue
            if not _in_scope(prop):
                # A neighbour county's listing surfaced by the search (or
                # re-hung by the town/ZIP cross-reference): not this report's
                # scope, so it never becomes a candidate.
                out_of_scope += 1
                continue
            append_record(CANDIDATES, {
                "id": int(zpid), "zpid": zpid, "url": prop.get("url"),
                "address": prop.get("address"), "city": prop.get("city"),
                "county": prop.get("county"), "state": prop.get("state"),
                "zip": prop.get("zip_code"),
                "latitude": prop.get("latitude"),
                "longitude": prop.get("longitude"),
                "price": prop.get("price"), "acres": prop.get("acres"),
                "property_type": prop.get("property_type"),
                "image_url": prop.get("image_url"),
                "swept_at": now,
            })
            existing[int(zpid)] = {"id": int(zpid)}
            new += 1
        added += new
        if challenged or pages == 0:
            # Do not mark a walled/empty county as swept: the next sweep
            # must retry it rather than silently keep a zero.
            logger.warning("  %s: no usable data (bot page=%s, pages=%d) — "
                           "left unrecorded so a rerun retries it",
                           county_display(county), challenged, pages)
            consecutive += 1
            if consecutive >= scraper.MAX_CONSECUTIVE_CHALLENGES:
                logger.error("forest report: %d counties walled — stopping; "
                             "rerun later", consecutive)
                break
            continue
        state[key] = {"county": county_display(county), "returned": returned,
                      "qualifying": len(props), "new": new, "pages": pages,
                      "challenged": challenged, "swept_at": now,
                      "out_of_scope": out_of_scope}
        _save_swept(state)
        logger.info("  %s: %d returned / %d qualifying / %d new "
                    "(%d page%s, %d out of scope)", county_display(county),
                    returned, len(props), new, pages,
                    "" if pages == 1 else "s", out_of_scope)
        consecutive = consecutive + 1 if challenged else 0
        if consecutive >= scraper.MAX_CONSECUTIVE_CHALLENGES:
            logger.error("forest report: %d consecutive bot pages — "
                         "stopping the sweep; rerun to continue",
                         consecutive)
            break
    logger.info("forest report: %d candidate(s) added this pass, %d known",
                added, len(existing))
    return added


def stage_details(args) -> int:
    """Render each candidate's detail page for its description."""
    _ensure_tmp()
    candidates, _out_of_scope = _candidates()
    done = {k: v for k, v in load_done(DETAILS).items() if v.get("ok")}
    todo = [c for c in candidates
            if int(c["id"]) not in done and c.get("url")]
    if args.limit_details:
        todo = todo[:args.limit_details]
    if not todo:
        logger.info("forest report: no descriptions to harvest")
        return 0
    scraper = ForestZillowScraper()
    logger.info("forest report: harvesting %d description(s) (%d done)",
                len(todo), len(done))
    found = 0
    for i, cand in enumerate(todo, 1):
        time.sleep(random.uniform(1.0, 2.0))
        desc, parcel = scraper._fetch_detail(cand["url"])
        if desc:
            found += 1
        append_record(DETAILS, {"id": int(cand["id"]),
                                "description": desc, "parcel": parcel,
                                "ok": bool(desc),
                                "fetched_at": int(time.time())})
        if i % 25 == 0:
            logger.info("  descriptions %d/%d — %d found",
                        i, len(todo), found)
    logger.info("forest report: %d/%d description(s) harvested",
                found, len(todo))
    return found


def stage_elevation(args) -> int:
    """Measure ground elevation for candidates that have coordinates.

    The product rule measures a parcel, not a pin, so the resolved parcel's
    centroid is used when the adjacency stage found one; the Zillow pin is
    the fallback.  A stored measurement is re-taken only when its basis
    point moved (so a re-run of the adjacency stage cannot leave a stale
    elevation behind).
    """
    _ensure_tmp()
    candidates, _out_of_scope = _candidates()
    done = load_done(ELEV_FILE)
    adj = load_done(ADJ_FILE)
    todo: list[tuple[dict, float, float, str]] = []
    for cand in candidates:
        zpid = int(cand["id"])
        adj_row = adj.get(zpid) or {}
        centroid = adj_row.get("subject_centroid")
        if centroid:
            lon, lat, basis = centroid[0], centroid[1], "centroid"
        elif cand.get("latitude") is not None and \
                cand.get("longitude") is not None:
            lat, lon, basis = (cand["latitude"], cand["longitude"], "pin")
        else:
            continue
        prev = done.get(zpid)
        if prev and prev.get("elevation_ft") is not None and \
                abs((prev.get("latitude") or 0) - lat) < 0.0005 and \
                abs((prev.get("longitude") or 0) - lon) < 0.0005:
            continue
        todo.append((cand, lat, lon, basis))
    if not todo:
        logger.info("forest report: no elevations to measure")
        return 0
    logger.info("forest report: measuring %d elevation(s)", len(todo))
    values = elevations([(lat, lon) for _, lat, lon, _ in todo])
    measured = 0
    for (cand, lat, lon, basis), value in zip(todo, values):
        if value is not None:
            measured += 1
        append_record(ELEV_FILE, {"id": int(cand["id"]),
                                  "elevation_ft": value,
                                  "latitude": lat, "longitude": lon,
                                  "basis": basis})
    logger.info("forest report: %d/%d elevation(s) measured",
                measured, len(todo))
    return measured


def adjacency_todo(candidates: list[dict], details: dict,
                   done: dict) -> list[dict]:
    """Candidates still needing a verdict.

    A verdict is re-judged when it was recorded *before* the description
    arrived (``desc_len`` differs), so an interrupted details pass can never
    leave a listing judged on GIS alone while its description says otherwise.

    A verdict recorded **before the acreage guard** is re-judged too when its
    subject is missing or implausible: those checkpoints bound listings to a
    neighbour's polygon (or to the parent parcel a new lot was carved from),
    and resolution now falls back to measuring the pin instead.  Records
    written by the current code carry ``subject_method``, so they are stable.
    """
    todo = []
    for cand in candidates:
        zpid = int(cand["id"])
        prev = done.get(zpid)
        desc = (details.get(zpid) or {}).get("description") or ""
        if prev is None or prev.get("desc_len", 0) != len(desc):
            todo.append(cand)
            continue
        if "subject_method" not in prev and not subject_is_plausible(
                prev.get("subject"), cand.get("acres")):
            todo.append(cand)
    return todo


def judge_verdict(why, nearest: Optional[dict], subject: Optional[dict],
                  tolerance_ft: float) -> tuple[str, Optional[str], bool]:
    """Decide a row's verdict — description first, then a POLYGON-only GIS test.

    Returns ``(verdict, source, gis_withheld)``.  A GIS claim is about a
    PARCEL, so it needs a parcel polygon: a pin-only subject has no
    acreage-plausible parcel (see :func:`resolve_subject_parcel`) and a
    geocode can drift hundreds of feet in the mountains, so its distance is
    recorded for triage but never becomes an adjacency.
    """
    if why.verdict == "adjacent":
        return "adjacent", "description", False
    if nearest and nearest.get("gap_ft") is not None \
            and nearest["gap_ft"] <= tolerance_ft:
        if (subject or {}).get("method") in POLYGON_METHODS:
            return "adjacent", "gis", False
        return "no", None, True
    if why.verdict == "mention":
        return "mention", None, False
    return "no", None, False


def stage_adjacency(args) -> int:
    """Description first, GIS second — verdict per candidate."""
    _ensure_tmp()
    candidates, _out_of_scope = _candidates()
    details = load_done(DETAILS)
    done = load_done(ADJ_FILE)
    todo = adjacency_todo(candidates, details, done)
    if not todo:
        logger.info("forest report: adjacency already resolved")
        return 0
    index, coverage = build_index(refresh=args.refresh_index)
    if len(index) == 0:
        raise RuntimeError(
            "forest index is empty — refusing to judge adjacency (a data "
            "outage would mark every listing non-adjacent); rerun with "
            "--refresh-index")
    gap_counties = {v["county"].lower() for v in coverage.values()
                    if v["parcels"] == 0}
    logger.info("forest report: %d public parcel(s) indexed (tolerance %.0f ft)",
                len(index), args.tolerance_ft)
    found = 0
    for i, cand in enumerate(todo, 1):
        zpid = int(cand["id"])
        detail = details.get(zpid) or {}
        desc = detail.get("description")
        why = description_evidence(desc)
        subj = None
        withheld = False
        try:
            subj = resolve_subject_parcel(
                cand.get("latitude"), cand.get("longitude"),
                address=cand.get("address"), county=cand.get("county"),
                parcel=detail.get("parcel"),
                expected_acres=cand.get("acres"))
        except Exception as exc:                # one bad parcel must not stop
            logger.warning("parcel resolution failed for %s: %s",
                           cand.get("url"), exc)
        geo = index.nearest((subj or {}).get("rings"),
                            lat=cand.get("latitude"),
                            lon=cand.get("longitude"),
                            tolerance_ft=args.tolerance_ft)
        nearest = None
        if geo and geo.get("parcel"):
            p = geo["parcel"]
            nearest = {"parno": p.parno, "owner": p.owner, "kind": p.kind,
                       "acres": p.acres, "county": p.county,
                       "gap_ft": geo["gap_ft"],
                       "geometry": geo["geometry"]}
        verdict, source, withheld = judge_verdict(why, nearest, subj,
                                                  args.tolerance_ft)
        if verdict == "adjacent":
            found += 1
        centroid = None
        if subj and subj.get("rings"):
            pts = [p for ring in subj["rings"] for p in ring]
            if pts:
                centroid = [round(sum(p[0] for p in pts) / len(pts), 6),
                            round(sum(p[1] for p in pts) / len(pts), 6)]
        append_record(ADJ_FILE, {
            "id": zpid, "verdict": verdict, "source": source,
            "description_evidence": why.as_dict(),
            "desc_len": len(desc or ""),
            "nearest_public": nearest,
            "subject_centroid": centroid,
            "subject": ({k: v for k, v in subj.items() if k != "rings"}
                        if subj else None),
            "subject_method": (subj or {}).get("method") or "none",
            # The pin came within the tolerance but no parcel polygon was
            # plausible: proximity is recorded, adjacency is withheld.
            "gis_withheld": withheld,
            "gis_coverage": ("none" if (cand.get("county") or "").lower()
                             in gap_counties else "ok"),
        })
        if i % 25 == 0:
            logger.info("  adjacency %d/%d — %d adjacent so far",
                        i, len(todo), found)
        time.sleep(0.5)
    logger.info("forest report: %d/%d adjacent", found, len(todo))
    return found


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _money(value) -> str:
    try:
        return f"${int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _num(value, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if digits <= 0:
        return f"{number:,.0f}"
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".")


def _evidence(row: dict) -> str:
    ev = row.get("description_evidence") or {}
    near = row.get("nearest_public") or {}
    if row.get("source") == "description":
        quote = (ev.get("quote") or "").strip()
        return f"“{quote[:150]}”"
    if row.get("source") == "gis" and near:
        gap = near.get("gap_ft")
        owner = near.get("owner") or "public land"
        acres = _num(near.get("acres"), 0)
        method = (row.get("subject") or {}).get("method")
        basis = "" if method in POLYGON_METHODS else " from the pin"
        return f"{owner} ({acres} ac), {_num(gap, 0)} ft away{basis}"
    if ev.get("verdict") == "mention":
        return "description mentions forest, not adjacency"
    return "—"


def _kind_label(kind: Optional[str]) -> str:
    """Human label for a public-land kind (used when the quote names none)."""
    return {"federal_forest": "national forest (unnamed in listing)",
            "federal_park": "national park (unnamed in listing)",
            "state": "state land (unnamed in listing)",
            "county": "county forest (unnamed in listing)",
            }.get(kind or "", "public land (unnamed in listing)")


# Street-type spellings a re-posting may change ("247 Rice Ln" → "247 Rice
# Lane LOT 4" was live: same parcel, owner, acreage and price, two zpids).
_SUFFIX_ALIASES = {
    "ln": "lane", "rd": "road", "st": "street", "dr": "drive",
    "trl": "trail", "tr": "trail", "hwy": "highway", "av": "avenue",
    "ave": "avenue", "blvd": "boulevard", "ct": "court", "cir": "circle",
    "pkwy": "parkway", "pl": "place", "cv": "cove", "holw": "hollow",
    "xing": "crossing", "mt": "mount", "crk": "creek", "br": "branch",
}

_LOT_TOKEN_RE = re.compile(r"\b(?:lot|lt|unit|ste)\s*[\w&-]+")
_NON_WORD_RE = re.compile(r"[^a-z0-9 ]")


def posting_key(row: dict) -> tuple:
    """Identity of a property across Zillow re-postings.

    A resolved subject parcel plus acreage and price is the strongest key
    (same tax parcel, same deal).  Without one, the street line is normalised
    — lot/unit tokens dropped, suffix abbreviations expanded — and paired
    with the acreage and price.
    """
    acres = round(float(row.get("acres") or 0), 2)
    price = float(row.get("price") or 0)
    parno = (row.get("subject") or {}).get("parno")
    if parno:
        return ("parcel", parno, acres, price)
    street = _LOT_TOKEN_RE.sub(" ",
                               street_only(row.get("address") or "").lower())
    street = _NON_WORD_RE.sub(" ", street)
    tokens = [_SUFFIX_ALIASES.get(t, t) for t in street.split()]
    return ("street", (row.get("county") or "").lower(),
            " ".join(tokens), acres, price)


def dedupe_adjacent(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapse the same property re-posted under a fresh Zillow id.

    Zillow re-lists a parcel with a new zpid and sometimes a variant street
    form, which would count one property twice in the headline number.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    dropped = 0
    for row in rows:
        key = posting_key(row)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(row)
    return out, dropped


def near_misses(qualified: list[dict], tolerance_ft: float,
                ceiling_ft: float = 500.0) -> list[dict]:
    """Qualified rows whose nearest public land is just past the tolerance.

    Not adjacency — a triage list.  The county roll sometimes leaves a sliver
    between two polygons and Zillow pins drift, so the 150–500 ft band is
    where a human should look before trusting a "no".
    """
    out = []
    for row in qualified:
        near = row.get("nearest_public") or {}
        gap = near.get("gap_ft")
        if gap is None or row.get("verdict") == "adjacent":
            continue
        if tolerance_ft < float(gap) <= ceiling_ft:
            out.append(row)
    out.sort(key=lambda r: r["nearest_public"]["gap_ft"])
    return out


def stage_report(args) -> int:
    """Join the checkpoints and write the markdown + JSON report."""
    candidates, _out_of_scope = _candidates()
    details = load_done(DETAILS)
    elev = load_done(ELEV_FILE)
    adj = load_done(ADJ_FILE)
    index, coverage = build_index(refresh=False)

    rows = []
    for cand in candidates:
        zpid = int(cand["id"])
        row = dict(cand)
        row["description"] = (details.get(zpid) or {}).get("description")
        row["elevation_ft"] = (elev.get(zpid) or {}).get("elevation_ft")
        row.update(adj.get(zpid) or {})
        row["id"] = zpid
        if row.get("state") and row["state"] != COUNTRY_STATE:
            continue
        rows.append(row)

    qualified = [r for r in rows
                 if r.get("elevation_ft") is not None
                 and r["elevation_ft"] >= args.min_elevation]
    adjacent = [r for r in qualified if r.get("verdict") == "adjacent"]
    adjacent.sort(key=lambda r: (r.get("county") or "",
                                 r.get("price") or 0))
    adjacent, duplicates = dedupe_adjacent(adjacent)

    by_county: dict[str, dict] = {}
    for row in rows:
        c = by_county.setdefault(row.get("county") or "?", {
            "swept": 0, "qualified": 0, "adjacent": 0})
        c["swept"] += 1
    for row in qualified:
        by_county.setdefault(row.get("county") or "?",
                             {"swept": 0, "qualified": 0, "adjacent": 0}
                             )["qualified"] += 1
    for row in adjacent:
        by_county.setdefault(row.get("county") or "?",
                             {"swept": 0, "qualified": 0, "adjacent": 0}
                             )["adjacent"] += 1

    desc_n = sum(1 for r in adjacent if r.get("source") == "description")
    gis_n = sum(1 for r in adjacent if r.get("source") == "gis")
    unknown_elev = sum(1 for r in rows if r.get("elevation_ft") is None)
    low_elev = sum(1 for r in rows if r.get("elevation_ft") is not None
                   and r["elevation_ft"] < args.min_elevation)
    mentions = sum(1 for r in qualified
                   if r.get("verdict") == "mention")
    # Pin-only proximity: the Zillow pin came within tolerance of public land
    # but no acreage-plausible parcel polygon was found, so no GIS adjacency
    # is claimed.  Listed separately as triage.
    pin_only = [r for r in qualified if r.get("gis_withheld")]
    pin_only.sort(key=lambda r: (r.get("nearest_public") or {})
                  .get("gap_ft") or 1e9)
    # Public land just past the tolerance: not adjacency, but the band a
    # human should re-check (roll slivers and pin drift both live here).
    close_calls = near_misses(qualified, args.tolerance_ft)
    desc_harvested = sum(1 for r in rows if r.get("description"))
    parcels_resolved = sum(1 for r in rows
                           if (r.get("subject") or {}).get("method")
                           in POLYGON_METHODS)
    point_only = sum(1 for r in rows
                     if ((r.get("nearest_public") or {}).get("geometry")
                         == "point"))
    gap_counties = sum(1 for c in coverage.values() if c["parcels"] == 0)

    lines: list[str] = []
    lines.append("# Forest-Adjacent Land — NC Mountains")
    lines.append("")
    lines.append(f"Generated {time.strftime('%Y-%m-%d %H:%M %Z')} · "
                 f"source: Zillow listings · "
                 f"{len(NC_MOUNTAIN_COUNTIES)} in-scope NC mountain counties.")
    lines.append("")
    lines.append("## Rule applied")
    lines.append("")
    lines.append(f"- **Acreage ≥ {_num(args.min_acres)}** "
                 f"(report override, not the dashboard's 1.1 ac).")
    lines.append(f"- **Price ≤ {_money(args.max_price)}**.")
    lines.append(f"- **Ground elevation ≥ {_num(args.min_elevation, 0)} ft** "
                 f"(USGS 3DEP, measured per parcel).")
    lines.append(f"- **Adjacent to public forest**: national, state or county "
                 f"land over **{MIN_PUBLIC_ACRES:.0f} acres** in NC OneMap, "
                 f"or a description that asserts adjacency to it.")
    lines.append("")
    lines.append("Evidence order: the listing description first "
                 "(“borders Pisgah National Forest”), then the GIS "
                 "neighbour test against the public parcel's own polygon. "
                 f"GIS adjacency tolerance is {_num(args.tolerance_ft, 0)} ft "
                 f"— calibrated to 0–5 ft on 64 topologically-adjacent "
                 f"control pairs, against a 1,195 ft median for random "
                 f"parcels.")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append("| Stage | Count |")
    lines.append("|---|---:|")
    lines.append(f"| Listings swept (≥{_num(args.min_acres)} ac, "
                 f"≤{_money(args.max_price)}) | {len(rows)} |")
    lines.append(f"| Zillow results outside the 15 counties (dropped) | "
                 f"{_out_of_scope} |")
    lines.append(f"| Descriptions harvested | {desc_harvested} / {len(rows)} |")
    lines.append(f"| Parcel polygons resolved | {parcels_resolved} / "
                 f"{len(rows)} |")
    lines.append(f"| GIS tests using a point only | {point_only} |")
    lines.append(f"| Elevation qualified (≥"
                 f"{_num(args.min_elevation, 0)} ft) | {len(qualified)} |")
    lines.append(f"| Elevation below threshold | {low_elev} |")
    lines.append(f"| Elevation unknown (no coordinates) | {unknown_elev} |")
    lines.append(f"| **Adjacent (description)** | **{desc_n}** |")
    lines.append(f"| **Adjacent (GIS)** | **{gis_n}** |")
    lines.append(f"| Same property re-posted (collapsed) | {duplicates} |")
    lines.append(f"| Forest mentioned, not adjacency | {mentions} |")
    lines.append(f"| Pin within tolerance, no parcel polygon (not counted) | "
                 f"{len(pin_only)} |")
    lines.append(f"| Public land 150–500 ft away (near miss) | "
                 f"{len(close_calls)} |")
    lines.append("")
    lines.append("## Adjacent properties")
    lines.append("")
    if not adjacent:
        lines.append("_No qualifying listing was adjacent to public forest._")
    else:
        lines.append("| County | Address | Acres | Price | Elev (ft) | "
                     "Adjacent to | Evidence | Listing |")
        lines.append("|---|---|---:|---:|---:|---|---|---|")
        for row in adjacent:
            near = row.get("nearest_public") or {}
            ev = row.get("description_evidence") or {}
            if row.get("source") == "description":
                # Name what the LISTING named.  Borrowing the GIS owner here
                # would put "USDA FOREST SERVICE" under a quote that only says
                # "borders national forest".
                target = ev.get("unit") or _kind_label(ev.get("kind"))
            else:
                target = near.get("owner") or "public forest"
            addr = row.get("address") or "—"
            city = row.get("city")
            if city and city.lower() not in addr.lower():
                addr = f"{addr}, {city}"
            url = row.get("url") or ""
            lines.append(
                f"| {row.get('county') or '—'} | {addr} | "
                f"{_num(row.get('acres'))} | {_money(row.get('price'))} | "
                f"{_num(row.get('elevation_ft'), 0)} | {target} | "
                f"{_evidence(row)} | [Zillow]({url}) |")
    lines.append("")
    if close_calls:
        lines.append("## Near misses (public land 150–500 ft away)")
        lines.append("")
        lines.append("Not counted as adjacent. These sit just outside the "
                     "tolerance — worth a manual look, because the county "
                     "roll sometimes leaves a sliver between polygons and "
                     "Zillow pins drift.")
        lines.append("")
        lines.append("| County | Address | Acres | Price | Elev (ft) | "
                     "Nearest public | Gap (ft) | Listing |")
        lines.append("|---|---|---:|---:|---:|---|---:|---|")
        for row in close_calls[:25]:
            near = row.get("nearest_public") or {}
            addr = row.get("address") or "—"
            city = row.get("city")
            if city and city.lower() not in addr.lower():
                addr = f"{addr}, {city}"
            lines.append(
                f"| {row.get('county') or '—'} | {addr} | "
                f"{_num(row.get('acres'))} | {_money(row.get('price'))} | "
                f"{_num(row.get('elevation_ft'), 0)} | "
                f"{near.get('owner') or '—'} "
                f"({_num(near.get('acres'), 0)} ac) | "
                f"{_num(near.get('gap_ft'), 0)} | "
                f"[Zillow]({row.get('url') or ''}) |")
        if len(close_calls) > 25:
            lines.append("")
            lines.append(f"_… {len(close_calls) - 25} more in the JSON "
                         f"output._")
        lines.append("")
    if pin_only:
        lines.append("## Pin-only proximity (not counted as adjacent)")
        lines.append("")
        lines.append("The Zillow pin came within the tolerance of public "
                     "land, but no parcel polygon of a plausible acreage "
                     "could be resolved — the roll offered a neighbour's lot "
                     "or a parent parcel far larger than the listing. A pin "
                     "is a geocode, not a boundary, so these are shown for "
                     "triage and never counted.")
        lines.append("")
        lines.append("| County | Address | Acres | Price | Elev (ft) | "
                     "Nearest public | Gap from pin (ft) | Listing |")
        lines.append("|---|---|---:|---:|---:|---|---:|---|")
        for row in pin_only[:15]:
            near = row.get("nearest_public") or {}
            addr = row.get("address") or "—"
            city = row.get("city")
            if city and city.lower() not in addr.lower():
                addr = f"{addr}, {city}"
            lines.append(
                f"| {row.get('county') or '—'} | {addr} | "
                f"{_num(row.get('acres'))} | {_money(row.get('price'))} | "
                f"{_num(row.get('elevation_ft'), 0)} | "
                f"{near.get('owner') or '—'} "
                f"({_num(near.get('acres'), 0)} ac) | "
                f"{_num(near.get('gap_ft'), 0)} | "
                f"[Zillow]({row.get('url') or ''}) |")
        if len(pin_only) > 15:
            lines.append("")
            lines.append(f"_… {len(pin_only) - 15} more in the JSON output._")
        lines.append("")
    lines.append("## By county")
    lines.append("")
    lines.append("| County | Swept | Elevation-qualified | Adjacent |")
    lines.append("|---|---:|---:|---:|")
    for county in sorted(by_county):
        c = by_county[county]
        lines.append(f"| {county} | {c['swept']} | {c['qualified']} | "
                     f"{c['adjacent']} |")
    lines.append("")
    notes = coverage_note(coverage)
    if notes:
        lines.append("## Data coverage")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")
    lines.append("## Method notes")
    lines.append("")
    lines.append("- Descriptions come from each Zillow detail page; the "
                 "search payload carries none. Sentences are judged one at a "
                 "time, so a forest name in the amenities paragraph cannot "
                 "borrow “borders” from the survey paragraph.")
    lines.append("- Elevation is measured by USGS 3DEP at the resolved "
                 "parcel's centroid when the GIS lookup finds it, otherwise "
                 "at the Zillow pin.")
    lines.append("- Distance marketing (“10 minutes from Pisgah”), denials "
                 "and bare mentions are recorded as *mentions*, never as "
                 "adjacency.")
    lines.append("- GIS adjacency is measured between the listing's **own "
                 "parcel polygon** and the public parcel polygon. A polygon "
                 "is used only when its acreage is plausible for the listing "
                 "(±40%): the roll will otherwise hand back a neighbour's lot "
                 "or the parent parcel a new lot was carved from, and that "
                 "polygon's forest line is not this listing's. When no "
                 "plausible polygon is found the pin's distance is recorded "
                 "but **no GIS adjacency is claimed** (a geocode is not a "
                 "boundary); those rows are listed under “Pin-only "
                 "proximity”.")
    lines.append("- NC OneMap's `IndexIntersects`/`EnvelopeIntersects` "
                 "relations test bounding boxes (a 214k-acre Great Smoky "
                 "polygon “intersects” most of Jackson County), so they are "
                 "deliberately not used.")
    lines.append("- Nothing here is written to the `properties` table; this "
                 "is a standalone report.")
    lines.append("")

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    payload = {
        "generated_at": int(time.time()),
        "rule": {"min_acres": args.min_acres, "max_price": args.max_price,
                 "min_elevation_ft": args.min_elevation,
                 "min_public_acres": MIN_PUBLIC_ACRES,
                 "adjacency_tolerance_ft": args.tolerance_ft,
                 "counties": [county_display(c)
                              for c in NC_MOUNTAIN_COUNTIES]},
        "counts": {"swept": len(rows), "qualified": len(qualified),
                   "adjacent_description": desc_n, "adjacent_gis": gis_n,
                   "mentions": mentions, "low_elevation": low_elev,
                   "unknown_elevation": unknown_elev,
                   "descriptions": desc_harvested,
                   "parcels_resolved": parcels_resolved,
                   "point_only_gis": point_only,
                   "near_misses": len(close_calls),
                   "pin_only": len(pin_only),
                   "duplicates_collapsed": duplicates,
                   "out_of_scope": _out_of_scope,
                   "counties_without_public_data": gap_counties},
        "coverage": coverage,
        "by_county": by_county,
        "properties": sorted(rows, key=lambda r: (
            r.get("county") or "", r.get("price") or 0)),
    }
    with open(REPORT_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    logger.info("forest report written: %s (%d adjacent of %d qualified)",
                REPORT_MD, len(adjacent), len(qualified))
    print(f"\n{len(adjacent)} adjacent of {len(qualified)} "
          f"elevation-qualified listing(s)")
    print(f"  markdown: {REPORT_MD}")
    print(f"  json    : {REPORT_JSON}")
    return len(adjacent)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forest-adjacency report over the in-scope NC mountain "
                    "counties (Zillow, ≥2 ac, ≤$300k, ≥1,700 ft).")
    parser.add_argument("--stage", default="all",
                        choices=["all", "sweep", "details", "elevation",
                                 "adjacency", "report"])
    parser.add_argument("--county", action="append", default=[],
                        help="limit the sweep (repeatable, e.g. --county "
                             "jackson)")
    parser.add_argument("--min-acres", type=float,
                        default=REPORT_MIN_ACRES)
    parser.add_argument("--max-price", type=int, default=REPORT_MAX_PRICE)
    parser.add_argument("--min-elevation", type=float,
                        default=REPORT_MIN_ELEVATION)
    parser.add_argument("--tolerance-ft", type=float,
                        default=ADJACENCY_TOLERANCE_FT)
    parser.add_argument("--limit-details", type=int, default=0)
    parser.add_argument("--refresh-index", action="store_true")
    parser.add_argument("--resweep", action="store_true",
                        help="re-walk counties already recorded as swept")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    logger.info("forest report: %s ac / $%s / %s ft / counties=%s",
                args.min_acres, args.max_price, args.min_elevation,
                args.county or "all NC mountain")

    if args.stage in ("all", "sweep"):
        stage_sweep(args)
    if args.stage in ("all", "details"):
        stage_details(args)
    # Adjacency before elevation: resolving the parcel gives the centroid
    # the elevation rule should measure (a Zillow pin often sits at the
    # street entrance, hundreds of feet below the parcel's high ground).
    if args.stage in ("all", "adjacency"):
        stage_adjacency(args)
    if args.stage in ("all", "elevation"):
        stage_elevation(args)
    if args.stage in ("all", "report"):
        stage_report(args)


if __name__ == "__main__":
    main()
