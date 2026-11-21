"""Forest adjacency: is a listing next to government forest land?

Two independent tests, in the order the product asks for them:

1. **The listing description.**  A sentence that names public forest land
   *and* asserts contact ("borders Pisgah National Forest", "adjacent to
   the national forest") is adjacency evidence.  Proximity marketing
   ("10 minutes from Pisgah") and denials are recorded, never accepted.

2. **The GIS neighbour check.**  NC OneMap's parcel layer carries the
   federal and state holdings themselves (``USDA FOREST SERVICE``,
   ``US FOREST SERVICE``, ``USFS``, ``UNITED STATES OF AMERICA``,
   ``STATE OF NORTH CAROLINA``, ``GREAT SMOKY MTN NATI`` …), so adjacency
   is measured against their polygons.  Only parcels over
   ``MIN_PUBLIC_ACRES`` count, per the product rule.

Why the distance is computed here instead of by the service: the layer's
``esriSpatialRelIndexIntersects`` / ``EnvelopeIntersects`` relations test
**bounding boxes**, and a 214,000-acre Great Smoky Mountains polygon
"intersects" most of Jackson County under them.  Strict ``Intersects`` is
topologically honest but misses forest edges where the county roll leaves a
sliver between polygons.  So the index fetches each county's public parcels
once, and :meth:`ForestIndex.nearest` measures the real polygon-to-polygon
gap client-side (densified boundary points in a small spatial grid, then a
fine refinement on the winning parcel).

Measured owner strings (2026-11-18, NC OneMap layer 1), including the typos
the counties actually publish, are matched by :func:`classify_public_owner`.
Coverage is not uniform: Buncombe, Clay and Transylvania publish no
government parcel over 200 acres at all, and Macon/Graham publish one each,
so a GIS "no" there means "no data", not "no forest" —
:func:`coverage_note` reports that per county.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

MIN_PUBLIC_ACRES = 200.0
ADJACENCY_TOLERANCE_FT = 150.0
FOOT_PER_DEG_LAT = 364_000.0

# Boundary sampling.  Public rings at 80 ft keep the index small; the
# winning parcel is re-sampled at 10 ft so the number in the report is not
# inflated by sampling.  Subject parcels at 40 ft are plenty for a 150 ft
# verdict.
PUBLIC_STEP_FT = 80.0
SUBJECT_STEP_FT = 40.0
REFINE_STEP_FT = 10.0
PUBLIC_POINTS_CAP = 8_000
SUBJECT_POINTS_CAP = 4_000

CELL_DEG = 0.003            # ~900 ft E/W, ~1,090 ft N/S at NC latitudes
NEIGHBOUR_CELLS = 1         # 3x3 cells ≈ ±1,000 ft, far past the tolerance

INDEX_VERSION = 1
INDEX_MAX_AGE_S = 30 * 24 * 3600
INDEX_DIR = os.path.join(os.path.dirname(__file__), "tmp", "forest_index")

# NC county FIPS for the 15 in-scope mountain counties (config.py's
# NC_MOUNTAIN_COUNTIES).  Rutherford (161) and Union (177) are deliberately
# absent: both were ruled out of the mountain scope (foothills / radius).
NC_COUNTY_FIPS: dict[str, str] = {
    "021": "Buncombe", "039": "Cherokee", "043": "Clay", "075": "Graham",
    "087": "Haywood", "089": "Henderson", "099": "Jackson", "111": "McDowell",
    "113": "Macon", "115": "Madison", "121": "Mitchell", "149": "Polk",
    "173": "Swain", "175": "Transylvania", "199": "Yancey",
}

PUBLIC_OWNER_SQL = (
    "ownname LIKE '%FOREST%' OR ownname LIKE '%USFS%' "
    "OR ownname LIKE '%AMERICA%' OR ownname LIKE '%UNITED STATES%' "
    "OR ownname LIKE '%STATE OF%' OR ownname LIKE '%NORTH CAROLINA%' "
    "OR ownname LIKE '%NC STATE%' OR ownname LIKE '%N.C.%' "
    "OR ownname LIKE '%STATE PARK%' OR ownname LIKE '%COUNTY%' "
    "OR ownname LIKE '%PARKWAY%' OR ownname LIKE '%GAME LAND%' "
    "OR ownname LIKE '%WILDLIFE%' OR ownname LIKE '%NATIONAL%'"
)


# ---------------------------------------------------------------------------
# Owner classification
# ---------------------------------------------------------------------------

def classify_public_owner(owner: Optional[str],
                          county: Optional[str] = None) -> Optional[str]:
    """Kind of public ownership for an NC OneMap ``ownname``, or None.

    Returns ``federal_forest`` / ``federal_park`` / ``federal`` /
    ``state`` / ``county``.  Strings are matched case-insensitively and
    tolerate the punctuation and typos the rolls carry ("U S  OF AMERICA
    FOREST SERVICE", "THE UNITED STATES OF AMERICIA", "N.C. STATE
    PARK-MEDOC MOUNTAIN").
    """
    if not owner:
        return None
    u = re.sub(r"\s+", " ", owner.upper()).strip().strip("'\"")
    if not u:
        return None
    # Tribal trust land is not public forest for this rule.
    if "EASTERN BAND" in u or "CHEROKEE INDIAN" in u:
        return None
    if any(t in u for t in ("FOREST SERVICE", "FOREST SVC", "USFS",
                            "NATIONAL FOREST", "US FOREST")):
        return "federal_forest"
    if any(t in u for t in ("NATIONAL PARK", "GREAT SMOKY", "PARKWAY",
                            "NATIONAL SEASHORE")):
        return "federal_park"
    if any(t in u for t in ("OF AMERICA", "UNITED STATES", "U S A",
                            "U.S.A", "USA ")):
        return "federal"
    if any(t in u for t in ("STATE OF NORTH CAROLINA", "STATE OF NC",
                            "NORTH CAROLINA STATE", "N.C. STATE",
                            "NC STATE", "STATE FOREST", "STATE PARK",
                            "GAME LAND", "WILDLIFE RESOURCES",
                            "STATE OF N C")):
        return "state"
    if county:
        c = re.sub(r"[^A-Z ]", "", county.upper()).strip()
        if c and (f"COUNTY OF {c}" in u or re.search(rf"\b{c}\s+COUNTY\b", u)
                  or f"{c} COUNTY" in u):
            return "county"
    if "COUNTY STATE OF" in u:      # "HALIFAX COUNTY STATE OF NO…"
        return "county"
    if u.startswith("COUNTY OF "):
        return "county"
    return None


# ---------------------------------------------------------------------------
# Description screening
# ---------------------------------------------------------------------------

# Named public land in and around the NC mountains.  A named unit is strong
# evidence; the generic terms work too but are more often marketing copy.
FOREST_UNITS = (
    "pisgah national forest", "nantahala national forest",
    "cherokee national forest", "sumter national forest",
    "chattahoochee national forest", "jefferson national forest",
    "great smoky mountains national park", "great smoky mountain national park",
    "blue ridge parkway", "appalachian trail",
    "dupont state forest", "pisgah state forest", "tuttle state forest",
    "mount mitchell state park", "grandfather mountain state park",
    "chimney rock state park", "south mountains state park",
    "lake james state park", "elk knob state park", "new river state park",
    "stone mountain state park", "pilot mountain state park",
    "gorges state park", "hanging rock state park", "jones lake state park",
    "panthertown wilderness", "shining rock wilderness",
    "middle prong wilderness", "joyce kilmer wilderness",
    "linville gorge wilderness", "southern nantahala wilderness",
    "ellicott rock wilderness", "cranberry wilderness",
)

FOREST_TERMS = (
    "national forest", "state forest", "county forest", "national park",
    "state park", "national recreation area", "game land", "game lands",
    "wildlife management area", "wildlife refuge", "conservation area",
    "public land", "public lands", "forest service", "u.s. forest service",
    "us forest service", "usfs", "forestry service", "watershed land",
)

ADJACENCY_TERMS = (
    "adjacent", "adjacent to", "adjoins", "adjoining", "adjoin",
    "borders", "bordering", "bordered", "border", "abuts", "abutting", "abut",
    "backs up to", "backing up to", "backs onto", "backing onto",
    "bounded by", "surrounded by", "shares a border", "shares a boundary",
    "property line with", "property line borders", "neighboring",
    "neighbouring", "inholding", "in the heart of", "within the boundaries",
    "inside the boundaries", "gateway to", "access directly into",
    "direct access to", "trail access",
)

# Boundary verbs prove contact no matter which side the forest is named on.
# The rest ("direct access to", "surrounded by", "in the heart of") are
# marketing-legs of the sentence: they only count when the forest itself
# follows them ("direct access to Pisgah National Forest"), never when they
# trail off into scenery ("direct access to nature").
STRONG_ADJACENCY = frozenset((
    "adjacent", "adjacent to", "adjoins", "adjoining", "adjoin",
    "borders", "bordering", "bordered", "border", "abuts", "abutting", "abut",
    "backs up to", "backing up to", "backs onto", "backing onto",
    "bounded by", "shares a border", "shares a boundary",
    "property line with", "property line borders", "neighboring",
    "neighbouring", "inholding",
))
WEAK_ADJACENCY_WINDOW = 70

# Proximity / drive-time marketing: a forest mention, not adjacency.
DISTANCE_TERMS = (
    "minutes from", "minute drive", "minutes to", "miles from", "miles to",
    "close to", "nearby", "near the", "proximity to", "short drive",
    "easy drive", "easy access", "convenient to", "access to the",
    "gateway", "surrounded by nature", "views of", "view of",
)

NEGATIONS = (
    "not adjacent", "not adjoining", "does not border", "doesn't border",
    "no national forest", "no state forest", "not bordering",
    "not surrounded by", "isn't adjacent", "not near",
)

# Scenery about the lake / the region is not a statement about the parcel.
# "Lake Hiwassee … 165 miles of shoreline, much of it surrounded by national
# forest" says nothing about what this lot touches, and the same sentence
# shape shows up for marinas, valleys and whole communities.
INDIRECT_SUBJECTS = (
    "much of it", "much of the", "much of this", "parts of", "portions of",
    "some of", "areas of", "shoreline", "surrounding area",
    "surrounding mountains", "the area is", "the region", "the community is",
)
INDIRECT_SUBJECT_PATTERNS = (
    # "a small gated community surrounded by national forest" — the gate,
    # not the lot, touches the forest.
    re.compile(r"\b(?:community|subdivision|neighborhood|neighbourhood|"
               r"resort|development)\b[^.;]{0,30}\b(?:surrounded|bordered|"
               r"ringed|adjoins|borders)\b"),
)
# Abbreviations whose full stop is not a sentence boundary.  "U.S. Forest
# Service" used to be split into "… by U.S." + "Forest Service land", which
# threw away the "bordered" half of the sentence and lost real adjacency.
_ABBREV_RE = re.compile(r"\b(?:u\.s\.a?|n\.c\.)", re.I)
_ABBREV_DOT = "\u2024"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_WS_RE = re.compile(r"\s+")


def split_sentences(text: str) -> list[str]:
    """Sentence split that keeps ``U.S.`` / ``N.C.`` abbreviations intact."""
    protected = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", _ABBREV_DOT),
                               text)
    return [part.replace(_ABBREV_DOT, ".")
            for part in _SENTENCE_SPLIT_RE.split(protected)]


def _forest_named(text: str) -> bool:
    return (_named_unit(text) is not None
            or any(term in text for term in FOREST_TERMS))


def _has_adjacency_term(sentence: str) -> bool:
    """Does the sentence assert contact with public land?

    Strong boundary verbs count on their own; the weak, marketing-shaped
    ones only when the forest name follows within a short window.
    """
    for term in ADJACENCY_TERMS:
        for match in re.finditer(rf"(?<![a-z]){re.escape(term)}(?![a-z])",
                                 sentence):
            if term in STRONG_ADJACENCY:
                return True
            tail = sentence[match.end():match.end() + WEAK_ADJACENCY_WINDOW]
            if _forest_named(tail):
                return True
    return False


@dataclass
class DescriptionEvidence:
    verdict: Optional[str] = None      # "adjacent" | "mention" | None
    kind: Optional[str] = None         # federal_forest / state / …
    unit: Optional[str] = None         # named public land, when given
    quote: Optional[str] = None

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "kind": self.kind,
                "unit": self.unit, "quote": self.quote}


def _kind_for_terms(text: str) -> Optional[str]:
    if any(t in text for t in ("national forest", "forest service",
                               "usfs", "u.s. forest service")):
        return "federal_forest"
    if any(t in text for t in ("national park", "national recreation area")):
        return "federal_park"
    if any(t in text for t in ("state forest", "state park", "game land",
                               "game lands", "wildlife management area",
                               "wildlife refuge")):
        return "state"
    if "county forest" in text:
        return "county"
    return "public"


def _named_unit(text: str) -> Optional[str]:
    for unit in FOREST_UNITS:
        if unit in text:
            return unit.title()
    return None


def description_evidence(text: Optional[str]) -> DescriptionEvidence:
    """Adjacency evidence from one listing description.

    Every sentence is judged on its own: a forest name in the amenities
    paragraph must not borrow "borders" from the survey paragraph.  Denials
    veto the sentence, and distance marketing downgrades it to a mention.
    """
    if not text or not text.strip():
        return DescriptionEvidence()
    low = _WS_RE.sub(" ", text.lower())
    mention: Optional[DescriptionEvidence] = None
    adjacent_hits: list[DescriptionEvidence] = []
    for sentence in split_sentences(low):
        sentence = sentence.strip()
        if not sentence:
            continue
        unit = _named_unit(sentence)
        has_forest = unit is not None or any(t in sentence for t in FOREST_TERMS)
        if not has_forest:
            continue
        if any(n in sentence for n in NEGATIONS):
            continue
        adjacent = _has_adjacency_term(sentence)
        distant = any(t in sentence for t in DISTANCE_TERMS)
        indirect = (any(t in sentence for t in INDIRECT_SUBJECTS)
                    or any(p.search(sentence)
                           for p in INDIRECT_SUBJECT_PATTERNS))
        kind = _kind_for_terms(sentence) if not unit else (
            "federal_forest" if "national forest" in unit.lower() else
            "federal_park" if "national park" in unit.lower() else
            "state" if any(w in unit.lower() for w in
                           ("state forest", "state park"))
            else "public")
        evidence = DescriptionEvidence(None, kind, unit, sentence[:300])
        if adjacent and not distant and not indirect:
            adjacent_hits.append(evidence)
        elif adjacent and (distant or indirect) and mention is None:
            # "borders the forest, minutes from town" is still adjacency;
            # "minutes from the forest" alone is not.  adjacency + distance
            # in one sentence is ambiguous → keep as a mention.  A sentence
            # whose subject is the lake/region is scenery, never the parcel.
            mention = DescriptionEvidence("mention", kind, unit,
                                          sentence[:300])
        elif mention is None:
            mention = DescriptionEvidence("mention", kind, unit,
                                          sentence[:300])
    if adjacent_hits:
        # Prefer a sentence that names the forest, so the report can say
        # which public land the listing touches.
        adjacent_hits.sort(key=lambda e: e.unit is None)
        best = adjacent_hits[0]
        best.verdict = "adjacent"
        return best
    return mention or DescriptionEvidence()


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _periodic(points: list[tuple[float, float]], step_ft: float,
              cap: int) -> list[tuple[float, float]]:
    """Densify a ring/rings to roughly *step_ft* spacing, capped by stride.

    The stride cap matters for federal tracts: a 156,000-acre parcel's
    boundary would otherwise contribute tens of thousands of points.
    """
    step = step_ft / FOOT_PER_DEG_LAT
    out: list[tuple[float, float]] = []
    for ring in points:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            out.append((x1, y1))
            n = int(max(abs(x2 - x1), abs(y2 - y1)) / step)
            for i in range(1, n):
                f = i / n
                out.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))
        if ring:
            out.append(ring[-1])
    if len(out) > cap:
        stride = -(-len(out) // cap)
        out = out[::stride]
    return out


def _bbox(rings: list[list[tuple[float, float]]]) -> tuple[float, float,
                                                           float, float]:
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    return min(xs), min(ys), max(xs), max(ys)


def point_in_rings(lon: float, lat: float,
                   rings: list[list[tuple[float, float]]]) -> bool:
    """Ray-cast containment across every ring (holes not distinguished)."""
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            if (y1 > lat) != (y2 > lat):
                x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if lon < x_at:
                    inside = not inside
    return inside


def _distance_ft(x1: float, y1: float, x2: float, y2: float,
                 kx: float) -> float:
    dx = (x2 - x1) * kx
    dy = y2 - y1
    return math.hypot(dx, dy) * FOOT_PER_DEG_LAT


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

@dataclass
class PublicParcel:
    parno: Optional[str]
    owner: str
    kind: str
    acres: float
    county_fips: str
    county: str
    siteadd: Optional[str] = None
    rings: list[list[tuple[float, float]]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"parno": self.parno, "owner": self.owner, "kind": self.kind,
                "acres": self.acres, "county_fips": self.county_fips,
                "county": self.county, "siteadd": self.siteadd,
                "rings": [[[round(x, 6), round(y, 6)] for x, y in r]
                          for r in self.rings]}

    @classmethod
    def from_dict(cls, d: dict) -> "PublicParcel":
        rings = [[(p[0], p[1]) for p in r] for r in d.get("rings") or []]
        return cls(d.get("parno"), d.get("owner") or "", d.get("kind") or "",
                   float(d.get("acres") or 0), d.get("county_fips") or "",
                   d.get("county") or "", d.get("siteadd"), rings)


class ForestIndex:
    """In-memory spatial index of one or more counties' public parcels."""

    def __init__(self) -> None:
        self.parcels: list[PublicParcel] = []
        self._cells: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
        self._bboxes: list[tuple[float, float, float, float]] = []

    # -- construction ------------------------------------------------------
    def add(self, parcel: PublicParcel) -> None:
        if not parcel.rings:
            return
        idx = len(self.parcels)
        self.parcels.append(parcel)
        self._bboxes.append(_bbox(parcel.rings))
        for x, y in _periodic(parcel.rings, PUBLIC_STEP_FT,
                              PUBLIC_POINTS_CAP):
            key = (int(math.floor(x / CELL_DEG)), int(math.floor(y / CELL_DEG)))
            self._cells.setdefault(key, []).append((x, y, idx))

    def extend(self, parcels: Iterable[PublicParcel]) -> None:
        for parcel in parcels:
            self.add(parcel)

    def __len__(self) -> int:
        return len(self.parcels)

    # -- query -------------------------------------------------------------
    def nearest(self, rings: Optional[list[list[tuple[float, float]]]],
                lat: Optional[float] = None, lon: Optional[float] = None,
                tolerance_ft: float = ADJACENCY_TOLERANCE_FT
                ) -> Optional[dict]:
        """Nearest public parcel > ``MIN_PUBLIC_ACRES`` to the subject.

        ``rings`` is the subject parcel polygon (preferred — a 20-acre
        parcel's centroid can be 1,000 ft from its own boundary).  When
        only a point is known the point is used and the result is marked
        ``geometry="point"`` so the report can say the test was weaker.

        Returns None when the index is empty; otherwise a dict with
        ``parcel``, ``gap_ft``, ``adjacent``, ``geometry``.  ``gap_ft`` may
        exceed the tolerance (the caller decides what to print).
        """
        if not self._cells:
            return None
        if rings:
            subject = _periodic(rings, SUBJECT_STEP_FT, SUBJECT_POINTS_CAP)
            geom = "parcel"
        elif lat is not None and lon is not None:
            subject = [(lon, lat)]
            geom = "point"
        else:
            return None
        if not subject:
            return None
        if lat is None:
            lat = sum(y for _, y in subject) / len(subject)
        kx = 1.0 / max(math.cos(math.radians(lat)), 1e-6)

        best: tuple[float, Optional[int]] = (float("inf"), None)
        best_xy: Optional[tuple[float, float]] = None
        # Widen the search when nothing is nearby: the report should say
        # "nearest public land 2,400 ft away", not "unknown".  (A point-only
        # subject has no polygon, so its nearest boundary may be further.)
        # Verdicts are unaffected — the tolerance test is unchanged.
        max_ring = 8 if not rings else 4
        ring = NEIGHBOUR_CELLS
        while True:
            for x, y in subject:
                cx = int(math.floor(x / CELL_DEG))
                cy = int(math.floor(y / CELL_DEG))
                for dx in range(-ring, ring + 1):
                    for dy in range(-ring, ring + 1):
                        for px, py, idx in self._cells.get((cx + dx, cy + dy),
                                                          ()):
                            d = _distance_ft(x, y, px, py, kx)
                            if d < best[0]:
                                best = (d, idx)
                                best_xy = (x, y)
            if best[1] is not None or ring >= max_ring:
                break
            ring += 1
        if best[1] is None:
            return {"parcel": None, "gap_ft": None, "adjacent": False,
                    "geometry": geom}
        parcel = self.parcels[best[1]]

        # Refine on the winning parcel with a fine boundary sample so the
        # reported gap is not inflated by the 80 ft index sampling.  Refining
        # from the single winning subject point keeps this O(60k) rather than
        # O(subject points x 60k), which for a 4,000-point subject would be
        # 240M distance computations per listing.
        gap = best[0]
        fine = _periodic(parcel.rings, REFINE_STEP_FT, 60_000)
        if fine and best_xy is not None:
            sx, sy = best_xy
            gap = min(_distance_ft(sx, sy, px, py, kx) for px, py in fine)

        # A point that falls INSIDE a public parcel is not "N hundred feet
        # from its edge" — a pin inside the boundary is contact.
        if geom == "point" and point_in_rings(subject[0][0], subject[0][1],
                                              parcel.rings):
            gap = 0.0
        return {"parcel": parcel, "gap_ft": round(gap, 1),
                "adjacent": gap <= tolerance_ft, "geometry": geom}


# ---------------------------------------------------------------------------
# Index build / cache
# ---------------------------------------------------------------------------

def _cache_path(fips: str) -> str:
    return os.path.join(INDEX_DIR, f"{fips}.json")


def load_cached_county(fips: str, max_age_s: float = INDEX_MAX_AGE_S
                       ) -> Optional[list[PublicParcel]]:
    path = _cache_path(fips)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    if time.time() - stat.st_mtime > max_age_s:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Forest index cache unreadable (%s): %s", path, exc)
        return None
    if blob.get("version") != INDEX_VERSION:
        return None
    return [PublicParcel.from_dict(d) for d in blob.get("parcels") or []]


def save_cached_county(fips: str, parcels: list[PublicParcel]) -> None:
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        with open(_cache_path(fips), "w", encoding="utf-8") as fh:
            json.dump({"version": INDEX_VERSION, "fips": fips,
                       "fetched_at": int(time.time()),
                       "parcels": [p.as_dict() for p in parcels]}, fh)
    except OSError as exc:
        logger.warning("Could not write forest index cache for %s: %s",
                       fips, exc)


def fetch_county_public_parcels(fips: str, county: str,
                                min_acres: float = MIN_PUBLIC_ACRES,
                                timeout: int = 120) -> list[PublicParcel]:
    """Every public parcel over *min_acres* in one county, with geometry."""
    from .nc_gis_lookup import _nc1map_query

    where = (f"cntyfips='{fips}' AND gisacres > {min_acres:.0f} "
             f"AND ({PUBLIC_OWNER_SQL})")
    feats = _nc1map_query({
        "where": where,
        "outFields": "parno,cntyname,cntyfips,ownname,gisacres,siteadd",
        "returnGeometry": "true",
        "outSR": "4326",
        "orderByFields": "gisacres DESC",
        "resultRecordCount": "1000",
        "f": "json",
    }, timeout=timeout)
    out: list[PublicParcel] = []
    seen: set[str] = set()
    for feat in feats or []:
        a = {k.lower(): v for k, v in (feat.get("attributes") or {}).items()}
        kind = classify_public_owner(a.get("ownname"), county)
        if not kind:
            continue
        acres = a.get("gisacres")
        if not isinstance(acres, (int, float)) or acres <= min_acres:
            continue
        geom = feat.get("geometry") or {}
        rings = [[(float(p[0]), float(p[1])) for p in r]
                 for r in (geom.get("rings") or []) if len(r) >= 4]
        if not rings:
            continue
        parno = (a.get("parno") or "").strip() or None
        # Dedupe on identity INCLUDING geometry: Cherokee publishes 104
        # public records under only 8 distinct parcel numbers (one PIN split
        # into many huge USFS blocks), so keying on parno alone collapsed
        # the county's entire national forest to a single polygon.
        key = (f"{parno or ''}|{a.get('ownname')}|{acres}|"
               f"{rings[0][0][0]:.6f},{rings[0][0][1]:.6f}")
        if key in seen:
            continue
        seen.add(key)
        out.append(PublicParcel(
            parno=parno, owner=str(a.get("ownname") or "").strip(),
            kind=kind, acres=float(acres), county_fips=fips,
            county=county, siteadd=a.get("siteadd"), rings=rings))
    return out


def build_index(counties: Optional[dict[str, str]] = None,
                refresh: bool = False, min_acres: float = MIN_PUBLIC_ACRES
                ) -> tuple[ForestIndex, dict[str, dict]]:
    """Load (or fetch and cache) the public-parcel index.

    Returns the index and a per-county coverage dict
    ``{fips: {"county", "parcels", "acres", "cached"}}`` so the report can
    state where a GIS "no" is a data gap rather than a real absence.
    """
    counties = counties or NC_COUNTY_FIPS
    index = ForestIndex()
    coverage: dict[str, dict] = {}
    for fips, county in counties.items():
        parcels = None if refresh else load_cached_county(fips)
        cached = parcels is not None
        if parcels is None:
            parcels = fetch_county_public_parcels(fips, county, min_acres)
            save_cached_county(fips, parcels)
            time.sleep(0.6)
        index.extend(parcels)
        coverage[fips] = {"county": county, "parcels": len(parcels),
                          "largest_acres": round(max((p.acres for p in
                                                      parcels), default=0), 1),
                          "cached": cached}
        logger.info("Forest index %s: %d public parcel(s) >%.0f ac",
                    county, len(parcels), min_acres)
    return index, coverage


# ---------------------------------------------------------------------------
# Subject parcel resolution
# ---------------------------------------------------------------------------

PARCEL_FIELDS = "parno,altparno,cntyname,cntyfips,ownname,gisacres,siteadd"


def _parcel_from_feat(feat: dict, county: Optional[str] = None
                      ) -> Optional[dict]:
    a = {k.lower(): v for k, v in (feat.get("attributes") or {}).items()}
    geom = feat.get("geometry") or {}
    rings = [[(float(p[0]), float(p[1])) for p in r]
             for r in (geom.get("rings") or []) if len(r) >= 4]
    if not rings:
        return None
    return {"parno": (a.get("parno") or "").strip() or None,
            "owner": (a.get("ownname") or "").strip() or None,
            "acres": a.get("gisacres"), "siteadd": a.get("siteadd"),
            "county": a.get("cntyname") or county,
            "rings": rings}


def _acreage_close(hit: Optional[dict], expected: Optional[float],
                   tolerance: float = 0.4) -> bool:
    """Is the resolved parcel's acreage plausible for the listing?"""
    if not hit or expected is None:
        return bool(hit)
    got = hit.get("acres")
    if not isinstance(got, (int, float)) or got <= 0:
        return True
    return abs(float(got) - expected) <= max(expected * tolerance, 0.5)


def resolve_subject_parcel(lat: Optional[float], lon: Optional[float],
                           address: Optional[str] = None,
                           county: Optional[str] = None,
                           parcel: Optional[str] = None,
                           expected_acres: Optional[float] = None
                           ) -> Optional[dict]:
    """Find the listing's own parcel polygon in NC OneMap.

    Order: known parcel number → the parcel containing the coordinate →
    address match.  The coordinate path runs before the address path because
    the address lookup issues a chain of LIKE queries (slow); it is still
    consulted when the coordinate lands in an implausible parcel (Zillow
    pins can drift into a neighbour, cf. #2739) or no polygon at all.

    Returns ``{parno, owner, acres, siteadd, county, rings, method}`` or None.
    """
    from .nc_gis_lookup import (_nc1map_query, _parcel_variants,
                                search_address_in_nc1map)

    county_key = (county or "").strip().lower().replace(" ", "_")
    fips = NC_COUNTY_FIPS_BY_NAME.get(county_key)

    def by_pin(pin: str, method: str) -> Optional[dict]:
        for variant in _parcel_variants(pin)[:6]:
            feats = _nc1map_query({
                "where": f"parno='{variant}'",
                "outFields": PARCEL_FIELDS,
                "returnGeometry": "true", "outSR": "4326",
                "resultRecordCount": "5", "f": "json",
            }, timeout=30)
            for feat in feats or []:
                got = _parcel_from_feat(feat, county)
                if got:
                    got["method"] = method
                    return got
        return None

    def by_point() -> Optional[dict]:
        if lat is None or lon is None:
            return None
        pad = 0.0015  # ~550 ft: catches the parcel without a county-wide scan
        feats = _nc1map_query({
            "geometry": json.dumps({"xmin": lon - pad, "ymin": lat - pad,
                                    "xmax": lon + pad, "ymax": lat + pad,
                                    "spatialReference": {"wkid": 4326}}),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "outFields": PARCEL_FIELDS,
            "returnGeometry": "true",
            "resultRecordCount": "40", "f": "json",
        }, timeout=30)
        hits = [p for p in (_parcel_from_feat(f, county) for f in feats or [])
                if p]
        inside = [p for p in hits if point_in_rings(lon, lat, p["rings"])]
        pick = (max(inside, key=lambda p: p.get("acres") or 0) if inside
                else None)
        if pick:
            pick["method"] = "point"
            return pick
        if hits:
            # The pin sits outside every parcel — a road right-of-way, or a
            # hole in the roll.  Use a hit only when its acreage matches the
            # listing: otherwise the polygon belongs to a neighbour, and its
            # forest line would be reported as this listing's.
            if expected_acres:
                hits.sort(key=lambda p: abs((p.get("acres") or 0)
                                            - float(expected_acres)))
                if _acreage_close(hits[0], expected_acres):
                    hits[0]["method"] = "point-near"
                    return hits[0]
            return None
        return None

    if parcel:
        got = by_pin(parcel, "parcel")
        if got:
            return got

    point_hit = by_point()
    if point_hit and point_hit["method"] == "point" \
            and _acreage_close(point_hit, expected_acres):
        return point_hit

    if address and fips:
        hit = search_address_in_nc1map(address, county)
        if hit and hit.get("parno"):
            addr_hit = by_pin(str(hit["parno"]), "address")
            if addr_hit and (not point_hit
                             or _acreage_close(addr_hit, expected_acres)
                             or not _acreage_close(point_hit, expected_acres)):
                return addr_hit

    return point_hit


# County name (as the app spells it) → FIPS.  config uses "van_buren" etc.,
# but the 15 in-scope NC counties are all single-word.
NC_COUNTY_FIPS_BY_NAME = {
    v.lower().replace(" ", "_"): k for k, v in NC_COUNTY_FIPS.items()
}


# ---------------------------------------------------------------------------
# Public-land coverage note
# ---------------------------------------------------------------------------

def coverage_note(coverage: dict[str, dict], min_parcels: int = 1,
                  sparse_below: int = 3) -> list[str]:
    """Markdown lines stating where the GIS test lacks public-parcel data."""
    lines: list[str] = []
    empty = [v["county"] for v in coverage.values()
             if v["parcels"] < min_parcels]
    if empty:
        lines.append(
            "**GIS coverage gap** — these counties publish no government-"
            "owned parcel over 200 acres in NC OneMap, so a GIS \"no\" there "
            "means *no data*, not *no forest*: " + ", ".join(sorted(empty))
            + ".")
    sparse = [f"{v['county']} ({v['parcels']})" for v in coverage.values()
              if min_parcels <= v["parcels"] < sparse_below]
    if sparse:
        lines.append(
            "**Sparse public-parcel coverage** — one or two public parcels "
            "over 200 acres are published in: " + ", ".join(sorted(sparse))
            + "; GIS adjacency there rests on very few polygons.")
    return lines
