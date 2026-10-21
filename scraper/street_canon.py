"""Shared street-name canonicalization for GIS address enrichment.

Listings spell compound street words out ("Hatcher Mountain Rd") while
parcel layers may abbreviate them ("HATCHER MTN RD 2869") — and vice versa
("WEBB CREEK" vs "webb crk"). Street-type suffixes (ST/RD/LN...) are
already stripped by each enricher's normalize step; this module covers the
compound DESCRIPTIVE words (mountain, valley, creek, ...) that survive.

Used by tnmap.py (TN statewide), nc_gis_lookup.py (NC OneMap), and
kgis_enrich.py (Knox KGIS) so every GIS enrichment applies the same
abbreviation matching plus a bounded "house number + first word or two"
fallback (enrichers must NOT search deeper than two street words on the
fallback tier — deeper matching binds wrong neighboring parcels).
"""
from __future__ import annotations
from typing import List

# canonical -> abbreviations / alternate spellings seen in parcel layers
STREET_VARIANTS: dict = {
    "mountain": ("mtn", "mt", "mnt", "mountains"),
    "valley": ("vly", "vld", "vally"),
    "creek": ("crk", "cr", "ck"),
    "branch": ("br", "brnch"),
    "church": ("ch", "chh"),
    "heights": ("hts",),
    "center": ("ctr", "cent", "centre", "cnter"),
    "junction": ("jct", "jction", "jctn"),
    "court": ("ct", "crt"),
    "square": ("sq", "sqr"),
    "meadow": ("mdw", "mdws", "meadows"),
    "mill": ("ml", "mills"),
    "ridge": ("rdg", "rdge"),
    "river": ("riv", "rver"),
    "spring": ("spg", "sprng", "sprngs", "springs", "spgs"),
    "willow": ("wllw",),
    "crossing": ("xing", "crossrd", "xng"),
    "corner": ("cor", "corners"),
    "camp": ("cp", "campground"),
    "grape": ("grp",),
    "leap": ("lp",),
    "knob": ("knb",),
    "orchard": ("orch",),
    "point": ("pt", "pts", "points"),
    "park": ("prk",),
    "plaza": ("plz",),
    "place": ("pl",),
    "terrace": ("ter", "terr"),
    "trail": ("trl",),
    "turnpike": ("tpke", "pky"),
    "parkway": ("pkwy",),
    "highway": ("hwy",),
    "green": ("grn",),
    "hollow": ("holw", "hollw"),
    "island": ("isl", "is"),
    "station": ("sta", "stn"),
}

# token -> canonical group key (reverse index, built once)
_CANON: dict = {alt: key for key, alts in STREET_VARIANTS.items()
                for alt in (key,) + alts}

# token -> alternates incl. the canonical key (forward index, built once)
_ALTS: dict = {}
for _key, _alts in STREET_VARIANTS.items():
    _ALTS[_key] = _alts
    for _a in _alts:
        _ALTS.setdefault(_a, [])
        _ALTS[_a] = list(dict.fromkeys(_ALTS[_a] + [_key]))


def canon_token(token: str) -> str:
    """Canonical group key for one street token ('mtn' -> 'mountain')."""
    return _CANON.get(token.lower(), token.lower())


def canon_tokens(street: str) -> List[str]:
    """Canonicalized token list for a street string."""
    return [canon_token(t) for t in (street or "").split()]


def canon_street(street: str) -> str:
    return " ".join(canon_tokens(street))


def alternate_tokens(token: str) -> List[str]:
    """Alternate spellings for a token, canonical key first when mapped.

    Alternates preserve the source token's case style (MTN retries as
    MOUNTAIN; mtn as mountain) so LIKE patterns read naturally."""
    alts = _ALTS.get(token.lower(), [])
    if token.isupper():
        return [a.upper() for a in alts]
    return alts


def tokens_match(set1, set2) -> bool:
    """Loose street-token equality via canonical keys (Jaccard >= 0.5)."""
    a = {canon_token(t) for t in set1}
    b = {canon_token(t) for t in set2}
    if not a or not b:
        return False
    common = len(a & b)
    return bool(common) and common / len(a | b) >= 0.5


def pattern_variants(pattern: str, limit: int = 6) -> List[str]:
    """Alternate LIKE patterns for a street pattern string (bounded).

    Replaces each known-variant token with its alternates, one word at a
    time, original first. Returns at most ``limit`` patterns so a search
    never more than doubles the ArcGIS request count.
    """
    parts = (pattern or "").split()
    outs: List[str] = []
    if pattern:
        outs.append(pattern)
    for i, w in enumerate(parts):
        for alt in alternate_tokens(w):
            cand = " ".join(parts[:i] + [alt] + parts[i + 1:])
            if cand not in outs:
                outs.append(cand)
            if len(outs) >= limit:
                return outs
    return outs
