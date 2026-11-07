"""Detect whether a listing describes an EXISTING building.

Zillow buckets listings with no usable dwelling data as ``LOT`` with NULL
bedrooms/bathrooms — but mountain parcels routinely carry an existing (often
uninhabitable) structure the listing never feeds as bed/bath facts. The
description text is the primary evidence; Zillow's own LLM "home insights"
tags and SSR facts (living area, year built) are supporting evidence.

Marketing copy is the trap: virtually every vacant-land description mentions
building a "dream home" (77/86 harvested GA lot descriptions matched a naive
keyword scan). This module therefore only reports an existing structure for
patterns that assert one is THERE (existing/older/as-is/damaged...), treats
build/site/dream language as a *future* signal, and leaves the remainder
ambiguous so a photo pass can adjudicate.

Pure functions only — no I/O — so scrapers and tests share one policy.
"""
from __future__ import annotations

import re
from typing import Optional

# Verdict tiers (also used by scraper.nc_building_harvest JSONL records).
HAS_BUILDING = "has_building"        # description/facts assert an existing structure
NO_BUILDING = "no_building"          # explicit vacant/unimproved language, no structure token
AMBIGUOUS = "ambiguous"              # unresolved — send to the photo pass

# A window between an anchor (old/with/…) and a structure token only proves
# PRESENCE when the gap does not read like future-construction copy ("room to
# build", "dream", "plans", "sites"). Checked in code so hyphenated existing
# compounds ("one-room cabin", "two-bedroom") stay evidence.
_GAP_BAD_RE = re.compile(
    r"\b(?:room|space)\s+(?:for|to|to\s+build)\b|\b(?:dream|sites?|potential"
    r"|plans?\b|build\w*|permitted|allowed|bring|future"
    r"|minimum|maximum|approved|proposed|heated|square\s+ft|sq\s?ft"
    r"|close\s+to|designed|ideal\s+for|great\s+for"
    r"|more|not|nearing|ideal|perfect|create|envision|handful|few|multiple|several|numerous"
    r"|upscale|executive|neighbor\w*|adjacent|nearby|surrounding"
    r"|primary|other|new)\b", re.I)

_PATTERN_MOD_TOKEN = re.compile(
    r"\b(?:existing|current|former|old|older|abandoned|decrepit|derelict"
    r"|rustic|seasonal|year[\s-]round)\b"
    r"([\w\s,'-]{0,30}?)\b"
    r"(?:cabins?|homes?|houses?|dwellings?|residences?|structures?"
    r"|buildings|storage\s+building|trailers?|manufactured|mobile\s+home"
    r"|foundations?|barns?|sheds?|carports?)\b"
    r"(?!\s*sites?\b)", re.I)

_PATTERN_WITH_TOKEN = re.compile(
    r"\b(?:with|has|having|features|featuring|includes?|including)\b"
    r"([\w\s,'-]{0,40}?)\b"
    r"(?:cabins?|homes?|houses?|dwellings?|residences?|structures?"
    r"|buildings|storage\s+building|trailers?|manufactured|mobile\s+home"
    r"|tiny\s+home|outbuildings?|barns?|foundations?)\b"
    r"(?!\s*sites?\b)(?!\s*pads?\b)", re.I)

# Words right before an anchor that flip the whole phrase to future/other
# copy ("make your home up higher", "between existing homes").
_ANCHOR_PRE_END_RE = re.compile(
    r"(?:make|creating?|build|building|put|place|dream|future|new|want\w*"
    r"|plan|planned|ready|could|can|would|should|between|among|besides"
    r"|pictures?|photos?|renders?|renderings?|imagine|envision|improving?)"
    r"(?:\s+(?:a|an|the|your|our|their|up|out|for))?\s*$", re.I)
# Cross-sell markers near an anchor: "See bordering acreage … with a cabin".
_CROSS_SELL_RE = re.compile(
    r"\bbordering\b|\badjoining\s+(?:acreage|parcel|lot|acres|property)"
    r"|see\s+MLS|\bat\s+MLS\s*\d|\(MLS\s*#", re.I)

# Anchors whose pre-window marks a cross-sell of a NEIGHBOURING property
# ("along with a home on 5.2 acres", "in combination with…").
_WITH_PRE_BAD_RE = re.compile(
    r"(?:along|comb(?:ined|ination)|conjunction|bundled|paired|together"
    r"|matched)\s*$"
    # "a range of home designs, including mobile homes" — a wish list.
    r"|(?:designs?|styles?|layouts?|varieties)\s*[,.]?\s*$", re.I)
# Post-window markers that flip a bare token to zoning copy ("homes of 800+
# sq ft permitted", "two homes allowed").
_TOKEN_POST_BAD_RE = re.compile(
    r"\b(?:permitted|allowed|minimum|maximum|max\s*\."
    r"|plans?\b|designs?\b|sizes?\b|for\s+access|restrict\w*)", re.I)
# A matched token that is part of a road name ("Goat House Way", "Spring
# House Road") is geography, not a structure.
_STREET_TOKEN_POST_RE = re.compile(
    r"\s*(?:way|rd|r?oad|st|street|ave|avenue|ln|lane|dr|drive|ct|court"
    r"|blvd|trl|trail|loop|hwy|highway|path|pl|place|circle|cir|pike)\b",
    re.I)

# "…with a manufactured home waiting to be moved off" — the home is leaving,
# not evidence of a structure for THIS sale's buyer.
_REMOVAL_POST_RE = re.compile(
    r"\b(?:waiting|to\s+be\s+moved|move\s+(?:them|it|off)|remove\w*|removal"
    r"|demolish\w*|relocat\w*)\b", re.I)


def _pre_window(text: str, start: int, width: int = 30) -> str:
    """Text before ``start``, clipped at the last sentence boundary."""
    w = text[max(0, start - width): start]
    for sep in (".", "!", "?", ";", "—"):
        i = w.rfind(sep)
        if i >= 0:
            w = w[i + 1:]
    return w


def _post_window(text: str, end: int, width: int = 40) -> str:
    """Text after ``end``, clipped at the first sentence boundary."""
    w = text[end: end + width]
    for sep in (".", "!", "?", ";", "—"):
        i = w.find(sep)
        if i >= 0:
            w = w[:i]
    return w

# Tokens that name a physical structure of any kind.
STRUCTURE_TOKEN_RE = re.compile(
    r"\b(?:cabins?|cottages?|bungalows?|farmhouses?|man\s+house|mansion"
    r"|shacks?|shanties?|dwellings?|residences?|home|homes|house|houses"
    r"|manufactured\s+(?:home|house)?|mfg\.?\s*home|mobile\s+home|modular\s+home"
    r"|trailers?|campers?|camper\s+trailer|rv\b|tiny\s+home|shepherd(?:'?s)?\s+hut"
    r"|structure|structures|building|buildings|improvements?|outbuildings?"
    r"|foundation|foundations|roof|roofs|barn|barns|shed|sheds)\b", re.I)

# Patterns asserting a structure EXISTS (present/on the land), including the
# uninhabitable vocabulary the bed/bath facts never carry.
EXISTING_PATTERNS = [
    # "cabin sits / home on the property / building in place" moved to
    # _sits_hits(): "The property sits on..." / "could be the spot where your
    # home sits" is land copy, so the containing sentence is vetted in code.
    # (located/situated variants moved to _located_hits: "the River House is
    # conveniently located nearby" is amenity copy.)
    # (in-place variants also moved into _PATTERN_SITS for sentence vetting.)
    re.compile(r"\b(?:built|construct\w*)\b[\w\s,-]{0,15}(?:in|circa|during)?\s*"
               r"(?:19|20)\d\d\b", re.I),
    re.compile(r"\byear\s+built\b", re.I),
    re.compile(r"\b(?:derelict|dilapidated|uninhabitable|decrepit|condemned"
               r"|fire[\s-]?damag\w*|storm[\s-]?damag\w*|hail[\s-]?damag\w*)"
               r"|\bneeds\s+(?:a\s+)?(?:lot\s+of\s+|some\s+|major\s+|minor\s+)?"
               r"(?:work|repairs?|restoration|rehab\w*|updating|a\s+new\s+roof|loving)\b", re.I),
    # Destruction language — the structure is there but not livable.
    # ("slab/chimney remains" moved to _remains_hits: conditional futures
    # like "until a foundation is in place" need a sentence check.)
    re.compile(r"\b(?:destroyed|collapsed|collapsing|burn(?:ed|t)\s+down"
               r"|came\s+down|washed\s+away)\b", re.I),
    re.compile(r"\bfixer[\s-]?upper?\b|\btear[\s-]?down\b", re.I),
    # "remodel/renovate" moved into _interior_hits(): "the newly renovated
    # resort down the road" is not evidence about THIS parcel.
    re.compile(r"\b(?:old|existing|new|metal|shingle|standing[\s-]seam)\s+roof\b", re.I),
    # Zoning boilerplate ("dwelling permitted") stays excluded. "tiny home" /
    # "camper" are context tokens only ("perfect for a tiny home" is future).
    # Habitation nouns (dwelling/residence/farmhouse...) moved to
    # _habitation_hits(): realtors print them in dream lists ("your dream ideas-
    # farmhouse, barndo") as often as on real homes, so each hit is checked
    # against its surrounding window in code.
    # The manufactured-home family is code-checked in _manufactured_hits():
    # sellers name the home they are selling, but copy also advertises the
    # POSSIBILITY ("you can put a mobile home on it").
    # Interior-room vocabulary only — vacant-land copy never shows a kitchen.
    # Moved to _interior_hits(): community clubhouses/pavilions have fireplaces
    # too, so each hit is checked against amenity context in code.
]

# Patterns that frame a dwelling as FUTURE (marketing) or deny one exists.
FUTURE_PATTERNS = [
    re.compile(r"\bdream\s+(?:home|house|cabin|retreat|escape)\b", re.I),
    re.compile(r"\b(?:build|building|built)\b[\w\s,'&-]{0,30}?\b"
               r"(?:home|house|cabin|cottage|retreat|nest|residence|dwelling)\w*\b",
               re.I),
    re.compile(r"\b(?:home|house|building|build|cabin|construction)\s+"
               r"(?:sites?|pads?)\b", re.I),
    re.compile(r"\b(?:homesite|building\s+space|space\s+for\s+(?:a|your)\s+"
               r"(?:home|house|cabin))\b", re.I),
    re.compile(r"\b(?:perfect|ideal|suited|establish|ready|waiting|room|space)\b"
               r"[\w\s,'&-]{0,30}?\b(?:for|to|as)\b[\w\s,'&-]{0,30}?\b"
               r"(?:home|house|cabin|building|build|residence|retreat|dwelling)\w*\b", re.I),
    re.compile(r"\bnew\s+construction\b|\bbring\s+your\s+(?:builder|plans?)\b", re.I),
    re.compile(r"\b(?:whether|planning|plans\s+for)\b[\w\s,'&-]{0,50}?\b"
               r"(?:home|house|cabin|retreat|residence)\w*\b", re.I),
    re.compile(r"\b(?:vacant|unimproved|undeveloped|undisturbed|raw)\s+"
               r"(?:lot|land|acreage|tract|parcel)\w*\b", re.I),
    re.compile(r"\bno\s+(?:existing\s+|current\s+|any\s+)?"
               r"(?:homes?|houses?|cabins?|structures?|buildings?|improvements?|dwelling)\b", re.I),
    re.compile(r"\bblank\s+slate\b", re.I),
]

# Subset strong enough to settle a parcel with NO structure word at all as
# vacant. Weak ones — bare "build a home", "room to build" — do not: they can
# accompany half-developed land ("utilities and septic already in place").
_STRONG_FUTURE_RES = [FUTURE_PATTERNS[i] for i in (0, 2, 3, 5, 7, 8, 9)]

# homeInsights phrases (Zillow's LLM tags) naming interior features — bare
# "home/house" tags are marketing noise and bare "hardwood" means timber
# forest on a mountain lot; both deliberately excluded.
INSIGHT_INTERIOR_RE = re.compile(
    r"\b(?:fireplace|kitchen|bedroom\w*|bathroom\w*|hardwood\s+floors?"
    r"|flooring|floor\s*plans?|vaulted|ceilings?|porch|deck|patio|cabinetry"
    r"|granite|appliances?|basement|single[\s-]wide|interior\s+(?:living"
    r"|finishes?))\b", re.I)

# Zillow's auto-generated first line on every listing, land included.
_ZILLOW_AUTOBOILER_RE = re.compile(
    r"\bThis home is located at\b.{0,120}?[.;]", re.I | re.S)

# Explicit vacancy markers used by the NO_BUILDING short-circuits.
VACANT_RE = re.compile(
    r"\b(?:vacant|unimproved|undeveloped|undisturbed|raw\s+land"
    r"|no\s+(?:existing\s+|current\s+|any\s+)?"
    r"(?:homes?|houses?|cabins?|structures?|buildings?"
    r"|improvements?|dwellings?)"
    r"|ready\s+to\s+build|building\s+site|home\s+site|homesites?"
    r"|(?:house|home|building)\s+pads?"
    r"|perk\w*\s+(?:filed|in\s+place|obtained|done|complete))\b", re.I)

# A structure mention that is really about a NEIGHBOURING listing's home or
# the zoning rules ("a home on adjoining acres", "2 homes allowed") is not
# evidence about THIS parcel.
_DISMISS_RE = re.compile(
    r"\b(?:neighbou?rs?|neighbou?ring|adjacent|nearby|surrounding|other)\b"
    r"|\boffered\s+separately\b|\bin\s+combination\b|\bMLS\s*#?\s*\d"
    r"|\b(?:you\s+)?can\s+(?:put|place|build)\b"
    r"|\b\d+\s+homes?\s+(?:are\s+)?(?:allowed|permitted)\b"
    r"|\b(?:homes?|structures?)\b[\w\s.,'+-]{0,25}\b(?:allowed|permitted)\b"
    # a house NEXT TO this acreage is the neighbour's; "up to 3-bedroom" is a
    # spec, not a built room.
    r"|\bnext\s+to\s+the\s+(?:acreage|lot|tract|parcel|property|land)\b"
    r"|\bup\s+to\s+(?:a\s+)?\d+\s*[-\s]?(?:bedroom|bath|sq\s?ft)\w*"
    r"|\bnearing\s+completion\b|\bunder\s+construction\b", re.I)

# Negated mentions ("no existing structures", "does not have a house") must
# not fire the EXISTING patterns; the gap may not cross has/with/sits so a
# nearby positive ("no HOA, has a cabin") survives.
_NEG_LIST = (r"homes?|houses?|cabins?|structures?|buildings?|improvements?"
             r"|dwellings?|trailers?|manufactured(?:\s+home)?|mobile\s+home"
             r"|outbuildings?|barns?|sheds?|roofs?|foundations?|damages?"
             r"|storm[\s-]?damag\w*|fire[\s-]?damag\w*|hail[\s-]?damag\w*"
             r"|campers?|camper\s+trailers?|rv'?s?|rvs?"
             r"|single[\s-]wide|double[\s-]wide")
_NEG_MODS = (r"(?:an?|any|existing|current|known|prior|evident|apparent"
             r"|previously|ever|signs?\s+of\s*|evidence\s+of\s*|history\s+of\s*)")
_NEG_CONT = (r"(?:\s*(?:,|and|or|nor)\s+(?:" + _NEG_MODS + r"\s*)*(?:"
             + _NEG_LIST + r")\w*)*")
_NEGATED_STRUCTURE_RE = re.compile(
    r"\b(?:no|without|zero|unrestricted\s+by|free\s+of|unencumbered\s+by"
    r"|never\s+had)\s+(?:" + _NEG_MODS + r"\s*){0,3}?"
    r"(?:" + _NEG_LIST + r")\w*" + _NEG_CONT
    + r"|\b(?:not\s+(?:have|having|including)|does\s+not\s+have)\b"
      r"[\w\s,'-]{0,12}?(?:" + _NEG_LIST + r")\w*" + _NEG_CONT
      # "has not been cleared or had a dwelling on it"
    + r"|\b(?:has|have|had)\s+not\b[\w\s,'-]{0,30}?\bhav(?:e|ed|ing)\s+"
      r"(?:a|an|any)?\s*(?:" + _NEG_LIST + r")\w*" + _NEG_CONT
    + r"|\bnot\b[\w\s,'-]{0,25}?\bhav(?:e|ed|ing)\s+(?:a|an|any)?\s*"
      r"(?:" + _NEG_LIST + r")\w*" + _NEG_CONT, re.I)


def _neutralize_negations(text: str) -> tuple[str, bool]:
    """Blank negated structure mentions and Zillow auto-boilerplate.

    A negated mention is itself vacancy evidence ("does not have a house").
    """
    text = _ZILLOW_AUTOBOILER_RE.sub(" ", text)
    scan, n = _NEGATED_STRUCTURE_RE.subn(" [NONE] ", text)
    return scan, n > 0


_PATTERN_HABITATION = re.compile(
    r"\bdwelling\b|\bresidence\b|\bfarmhouse\b|\bbungalow\b|\bman\s+house\b"
    r"|\bshepherd(?:'?s)?\s+hut\b", re.I)
_HABIT_FUTURE_BEFORE = re.compile(
    r"\b(?:ideal|perfect|dream\w*|create|envision|ideas|build\w*|barndominiums?"
    r"|barndos?|want\w*|looking\s+for|retreats?|ready|new|suited|planning"
    r"|establish|locate|situate|mandate\w*|require\w*|minimum"
    r"|between|among|potential)\b", re.I)
_HABIT_FUTURE_AFTER = re.compile(
    r"\b(?:permitted|allowed|restricted|sites?|for\s+your)\b", re.I)
# Realtors print dwelling/residence in dream lists as often as on real homes,
# so a habitation noun counts only when the sentence also asserts presence.
_HABIT_STRONG_PRE_RE = re.compile(
    r"\b(?:existing|current|former|older|old|derelict|abandoned|rustic"
    r"|remains?|remaining|left|standing|permanently|installed"
    r"|(?:19|20)\d\d|there\s+is|there\s+are|is\s+on"
    # "sits" only counts with a structure as its subject — "this lot sits
    # in the section" is land copy about the parcel, not a dwelling.
    r"|\b(?:house|home|cabin|dwelling|building|trailer)s?\b"
    r"[\w\s,'/-]{0,30}?\bsits?\b"
    r"|the\s+(?:dwelling|residence)\s+(?:is|has|was|sits))\b", re.I)

# "as is" is only structure evidence when a structure word is in the same
# window (vacant parcels also sell "sold as is"); "as is where is" asserts
# contents by itself.
_ASIS_WHERE_IS_RE = re.compile(r"\bas\s+is\s+where\s+is\b", re.I)
_ASIS_TOKEN = (r"cabin|home|house|structure|building|trailer|improvement\w*"
               r"|manufactured|mobile\s+home|roof|foundation")
_PATTERN_ASIS_NEAR = [
    re.compile(r"\bas[\s-]?is\b([\w\s,'-]{0,50}?)(?:" + _ASIS_TOKEN + r")\w*",
               re.I),
    # (?<=…) so "oasis is" / "Asia is" cannot read as the letters "as is".
    re.compile(r"\b(?:" + _ASIS_TOKEN + r")\w*([\w\s,'-]{0,50}?)"
               r"(?<=[\s(-])as[\s-]?is\b", re.I),
]

def _habitation_hits(text: str) -> list[re.Match]:
    """Habitation-noun hits whose sentences assert the dwelling is there."""
    hits = []
    for m in _PATTERN_HABITATION.finditer(text):
        before = text[max(0, m.start() - 45): m.start()]
        after = text[m.end(): m.end() + 20]
        if _HABIT_FUTURE_BEFORE.search(before):
            continue
        if _HABIT_FUTURE_AFTER.search(after):
            continue
        if not _HABIT_STRONG_PRE_RE.search(
                _sentence(text, m.start(), m.end())):
            continue
        hits.append(m)
    return hits


_PATTERN_INTERIOR = re.compile(
    r"\bfireplace\b|\bkitchen\w*\b|\bbathroom\w*\b"
    r"|\bhardwood\s+floors?\b|\bfloor\s*plans?\b|\bfurnish\w*\b"
    r"|\b(?:remodel\w*|renovat\w*)\b", re.I)
# Community facilities and comps — a clubhouse fireplace or "the renovated
    # resort down the road" says nothing about THIS parcel.
_AMENITY_CTX_RE = re.compile(
    r"\b(?:club\s?house|pavilion|resort|recreation\s+center|activity\s+center"
    r"|amenit\w+|equestrian|fitness\s+room|community\b|subdivision"
    r"|neighborhood|also\s+features"
    r"|surrounding\s+area|comparable|comps?)\b", re.I)


def _interior_hits(text: str) -> list[re.Match]:
    """Interior-room hits outside community-amenity context."""
    hits = []
    for m in _PATTERN_INTERIOR.finditer(text):
        # "outdoor kitchen / outdoor shower" is patio-amenity copy; on land
        # copy it describes a lifestyle, and the parcel's real structure —
        # if any — needs a stronger anchor than that.
        if re.search(r"\boutdoor\s+$|\boutside\s+$",
                     text[max(0, m.start() - 12):m.start()], re.I):
            continue
        if _AMENITY_CTX_RE.search(_sentence(text, m.start(), m.end())):
            continue
        if _future_veto(text, m.start()):
            continue
        hits.append(m)
    return hits


# "slab/chimney/foundation remains" — but a CONDITIONAL future foundation
# ("until a permanent foundation is in place") is not a present structure.
_PATTERN_REMAINS = re.compile(
    r"\b(?:slab|chimney|foundation|footings?)\b[\w\s,&-]{0,25}"
    r"\b(?:remain\w*|left|in\s+place|present|sitting|standing|stands?)\b",
    re.I)
_REMAINS_COND_RE = re.compile(
    r"\b(?:until|unless|before|cannot|can\s'?t|will\s+be|if)\b", re.I)


def _remains_hits(text: str) -> list[re.Match]:
    hits = []
    for m in _PATTERN_REMAINS.finditer(text):
        sent = _sentence(text, m.start(), m.end())
        if _REMAINS_COND_RE.search(sent[:m.start() - _sentence_start(
                text, m.start())]):
            continue
        hits.append(m)
    return hits


# Structures that belong to a NEIGHBOURING listing (MLS cross-sell, "the home
# adjacent to this land") are never evidence about this parcel.
_CROSS_LISTING_RE = re.compile(
    r"\b(?:MLS\s*#?\s*\d|see\s+MLS|adjacent|neighbou?ring|offered\s+separately"
    r"|in\s+combination|bordering)\b", re.I)


_MOD_POST_BAD_RE = re.compile(
    r"\b(?:or\b|sites?\b|livings?\b|lifestyles?\b|vacation|getaway"
    r"|retreats?\b|possibilit\w*)", re.I)


def _gap_pattern_hits(text: str) -> list[re.Match]:
    """Modifier/with-window hits: gap clean, anchor windows clean, no
    cross-sell idiom nearby."""
    hits = []
    for pat in (_PATTERN_MOD_TOKEN, _PATTERN_WITH_TOKEN):
        for m in pat.finditer(text):
            if _GAP_BAD_RE.search(m.group(1)):
                continue
            pre = _pre_window(text, m.start(), 40)
            if _ANCHOR_PRE_END_RE.search(pre):
                continue
            if _WITH_PRE_BAD_RE.search(pre[-30:]):
                continue
            if _CROSS_SELL_RE.search(text[max(0, m.start() - 90):
                                          m.end() + 40]):
                continue
            win = text[max(0, m.start() - 90): m.end() + 40]
            if _AMENITY_CTX_RE.search(win) or _CROSS_LISTING_RE.search(win):
                continue
            if re.search(r"\b(?:other|others|several|many|few|more|numerous)\b",
                         _pre_window(text, m.start(), 25), re.I):
                continue
            if _TOKEN_POST_BAD_RE.search(_post_window(text, m.end(), 40)):
                continue
            if _STREET_TOKEN_POST_RE.match(text[m.end():m.end() + 12]):
                continue
            if _REMOVAL_POST_RE.search(text[m.end():m.end() + 45]) \
                    and not _MANU_STRONG_PRE_RE.search(pre):
                continue
            if (pat is _PATTERN_MOD_TOKEN and
                    _MOD_POST_BAD_RE.search(text[m.end():m.end() + 22])):
                continue  # "year-round home or getaway" is an ad, not a home
            if _future_veto(text, m.start()):
                continue
            hits.append(m)
    return hits


_PATTERN_LOCATED = re.compile(
    r"\b(?:cabin|home|house|trailer|manufactured\s+home|mobile\s+home"
    r"|tiny\s+home|camper|structure|building|barn|shack)s?\b"
    r"[\w\s,'-]{0,30}?\b(?:located|situated)\b", re.I)
# "located nearby / downtown / minutes of town" describes amenities, not a
# structure on THIS parcel; "located at 742 X Rd / on the ridge" is placement.
_LOCATED_NEARBY_BAD_RE = re.compile(
    r"\b(?:nearby|downtown|close\b|minutes|walking|shopping|convenience"
    r"|restaurants|schools|village|town\b|airport|grocery)", re.I)


def _located_hits(text: str) -> list[re.Match]:
    """'home is located …' anchors vetted for amenity-vs-placement meaning."""
    hits = []
    for m in _PATTERN_LOCATED.finditer(text):
        if _ANCHOR_PRE_END_RE.search(_pre_window(text, m.start(), 25)):
            continue
        if _LOCATED_NEARBY_BAD_RE.search(_post_window(text, m.end(), 30)):
            continue
        hits.append(m)
    return hits


_PATTERN_BEDROOM = re.compile(r"\bbedrooms?\b", re.I)
_BED_PRE_BAD_RE = re.compile(
    r"\b(?:perk\w*|perc\b|permit\w*|approved|proposed|ready|septic|mound"
    r"|engineered|install\w*|size|sized|designed|sites?|plans?|up\s+to"
    r"|conventional)\b"
    # Sizing/fee copy: "$1350/yr" before "bedroom", "3/4" ratios — the $ and
    # slash live outside the \b wrapper because neither is a word character.
    r"|\$\s*\d|\d+\s*/", re.I)
_BED_POST_BAD_RE = re.compile(
    r"\b(?:septic|perc\b|tank|system|conventional|mound|perk\w*|permits?"
    r"|fee)\b", re.I)
# Septic-design sentences size a FUTURE house ("designed for a four-bedroom
# main residence, two one-bedroom cottages") — reject bedroom hits in them.
_BED_SENT_BAD_RE = re.compile(
    r"\b(?:septic|perk\w*|perc\b|permits?|designed|mound|engineered)\b",
    re.I)


def _bedroom_hits(text: str) -> list[re.Match]:
    """'bedroom' as a room, not septic-permit sizing copy."""
    hits = []
    for m in _PATTERN_BEDROOM.finditer(text):
        if _BED_PRE_BAD_RE.search(_pre_window(text, m.start(), 32)):
            continue
        if _BED_POST_BAD_RE.search(_post_window(text, m.end(), 25)):
            continue
        if _BED_SENT_BAD_RE.search(_sentence(text, m.start(), m.end())):
            continue
        hits.append(m)
    return hits


def _asis_hits(text: str) -> list[re.Match]:
    """as-is hits that share a window with a structure word."""
    if _ASIS_WHERE_IS_RE.search(text):
        return [m for m in _ASIS_WHERE_IS_RE.finditer(text)]
    hits = []
    for pat in _PATTERN_ASIS_NEAR:
        for m in pat.finditer(text):
            if not _GAP_BAD_RE.search(m.group(1)):
                hits.append(m)
    return hits


_PATTERN_MANU = re.compile(
    r"\b(?:manufactured\s+home|mobile\s+home|mfg\.?\s*home|modular\s+home"
    r"|trailer|single[\s-]wide|double[\s-]wide)\b", re.I)
# Sellers name the home they are selling; copy also advertises the POSSIBILITY
# ("you can put a mobile home on it", "waiting to be moved off"). A strong
# presence marker keeps the mention, any future wording in the SENTENCE before
# it (or a removal note right after) drops it.
_MANU_STRONG_PRE_RE = re.compile(
    r"\b(?:existing|older|on\s+(?:its\s+)?slab|permanently|situated)\b"
    # "a stick-BUILT home can be placed here" is future copy — built only
    # counts with a date/preposition ("built in 1998", "built on slab").
    r"|\bbuilt\s+(?:in|on|by|circa)\b|\byear[\s-]built\b|\b19\d\d|\b20[012]\d\b",
    re.I)
_MANU_FUTURE_RE = re.compile(
    r"\b(?:can|could|would|put|place|park\w*|room|space|allow\w*|permit\w*"
    r"|suitable|ideal|perfect|dream\w*|future|possible|easily|bring|want\w*"
    r"|ready\b|planning|whether|set\s+up|setting\b|no\s+restrictions"
    r"|willing)\b", re.I)
_MANU_FUTURE_POST_RE = re.compile(
    r"\b(?:waiting|to\s+be\s+moved|move\s+(?:them|it|off)|remove\w*|removal"
    r"|demolish\w*|relocat\w*)\b", re.I)
_MANU_POST_BAD_RE = re.compile(
    r"\b(?:not\s+(?:permitted|allowed)|permitted|allowed|prohibited"
    r"|restrict\w*|only|delivery|contractor|prep\w*)\b", re.I)


def _manufactured_hits(text: str) -> list[re.Match]:
    """Manufactured-home mentions the surrounding copy asserts as present."""
    hits = []
    for m in _PATTERN_MANU.finditer(text):
        left = max(0, m.start() - 120)
        seg = text[max(0, m.start() - 200):m.start()]
        for sep in (".", "!", "?", "\n"):
            i = seg.rfind(sep)
            if i >= 0:
                cand = m.start() - (len(seg) - i - 1)
                if cand > left:
                    left = cand
        pre = text[left:m.start()]
        # Sentence-clipped, so the wider reach stays inside this sentence:
        # "Single-wide manufactured homes and yurts are not permitted".
        if _MANU_POST_BAD_RE.search(_post_window(text, m.end(), 48)):
            continue
        if _MANU_STRONG_PRE_RE.search(pre):
            hits.append(m)
            continue
        if _MANU_FUTURE_RE.search(pre):
            continue
        if _MANU_FUTURE_POST_RE.search(text[m.end():m.end() + 45]):
            continue
        hits.append(m)
    return hits


_PATTERN_SPATIAL = re.compile(
    r"\b(?:structure|building|outbuilding|cabin|home|house|barn|trailer|shed)\w*"
    r"\b[\w\s,'-]{0,25}?\b(?:behind|beside|besides|next\s+to|adjacent\s+to"
    r"|in\s+front\s+of|underneath|alongside)\b", re.I)


# "Included in the sale are a 2016 Jayco camper…", "includes an RV with a
# full addition", "features a storage building" — inclusion anchors assert
# the item conveys with the parcel.
_PATTERN_INCLUSION = re.compile(
    r"\b(?:includes?|included(?:\s+in\s+the\s+sale)?\b[\w\s,']{0,20}?"
    r"|features|featuring|boasts|comprises)\b"
    r"([\w\s,'/-]{0,45}?)"
    r"\b(?:cabins?|homes?|houses?|trailers?|campers?|rv\b|airstream"
    r"|barns?|sheds?|carports?|outbuildings?|storage\s+buildings?)\b", re.I)
_INCL_POST_BAD_RE = re.compile(
    r"\s*(?:hookups?|pads?|sites?|spaces?|rooms?|connections?"
    r"|permits?|capable|ready|for\s+your)\b", re.I)


def _inclusion_hits(text: str) -> list[re.Match]:
    """Inclusion-anchor hits for concrete conveyable structures."""
    hits = []
    for m in _PATTERN_INCLUSION.finditer(text):
        if _GAP_BAD_RE.search(m.group(1)):
            continue
        if _INCL_POST_BAD_RE.search(text[m.end():m.end() + 25]):
            continue
        if _STREET_TOKEN_POST_RE.match(text[m.end():m.end() + 12]):
            continue
        window = text[max(0, m.start() - 90): m.end() + 40]
        if (_AMENITY_CTX_RE.search(window)
                or _CROSS_LISTING_RE.search(window)):
            continue
        if _future_veto(text, m.start()):
            continue
        hits.append(m)
    return hits


def _spatial_hits(text: str) -> list[re.Match]:
    """"structure behind the home"-style placement asserts presence.
    Neighbour-count copy ("Many beautiful homes next to the acreage") and
    future compounds ("dream home next to the creek") are rejected."""
    hits = []
    for m in _PATTERN_SPATIAL.finditer(text):
        pre = _pre_window(text, m.start(), 45)
        if _HABIT_FUTURE_BEFORE.search(pre):
            continue
        if re.search(r"\b(?:many|several|few|other|nearby|adjacent|new"
                     r"|existing|two|three|four|more)\b", pre, re.I):
            continue
        if re.match(r"\s*(?:the\s+)?(?:acreage|lot|tract|parcel|property"
                    r"|land)\b", text[m.end():m.end() + 30], re.I):
            continue
        # "house foundation sitting next to a stream" is the ruins tier —
        # the remains collector adjudicates it, not a placement claim.
        if re.search(r"\b(?:foundation|slabs?|remains?|ruins?|chimney"
                     r"|pads?)\b", m.group(0), re.I):
            continue
        # "...forest land adjacent to two developed parcels" / cross-listing
        # idioms anywhere in the sentence — the structure is not here.
        if _CROSS_LISTING_RE.search(_sentence(text, m.start(), m.end())):
            continue
        if _future_veto(text, m.start()):
            continue
        hits.append(m)
    return hits


_PATTERN_SITS = re.compile(
    r"\b(?:cabin|home|house|trailer|manufactured\s+home|mobile\s+home"
    r"|tiny\s+home|camper|structure|building|barn|shack|improvement)s?\b"
    r"([\w\s,'/-]{0,30}?)"
    r"\b(?:sits?|standing|in\s+place"
    r"|on\s+(?:the\s+)?(?:site|property|lot|parcel|land|acreage))\b", re.I)
# Land copy steals the same verbs: "the property sits on 5 acres", "where
# your home could sit". A future/spec marker anywhere in the SENTENCE vetoes.
_SENTENCE_FUTURE_RE = re.compile(
    r"\b(?:build\w*|could|would|make|put"
    r"|place\s+(?:a|an|the|your|on|in|at|it|them|here|there|for)"
    r"|picture[sd]?\b|photo\w*"
    r"|render\w*|dream\w*|whether|bring|imagine\w*|envision\w*|creat\w*"
    r"|willing|plans?\b|planning\b|planned\b|designed\b"
    r"|proposed|seeking|searching|until|between|nearby|remove\w*"
    r"|move\s+(?:them|it|off))\b", re.I)


def _sentence_start(text: str, pos: int) -> int:
    """Start of the [.!?] sentence containing ``pos`` (±220 chars max)."""
    left = max(0, pos - 220)
    seg = text[left:pos]
    for sep in (".", "!", "?", "\n"):
        i = seg.rfind(sep)
        if i >= 0:
            cand = pos - (len(seg) - i - 1)
            if cand > left:
                left = cand
    return left


def _future_veto(text: str, start: int) -> bool:
    """Future-intent wording BEFORE the anchor inside its own sentence.

    A dream verb after the anchor ("a barn sits here, room to build") does
    not negate what already precedes it; one before ("build … with a barn")
    frames the whole phrase as marketing.
    """
    return bool(_SENTENCE_FUTURE_RE.search(
        text[_sentence_start(text, start):start]))


def _sentence(text: str, start: int, end: int) -> str:
    """The [.!?] sentence containing [start:end] (±220 chars max)."""
    left = max(0, start - 220)
    seg = text[max(0, start - 220):start]
    for sep in (".", "!", "?", "\n"):
        i = seg.rfind(sep)
        if i >= 0:
            cand = start - (len(seg) - i - 1)
            if cand > left:
                left = cand
    right = min(len(text), end + 220)
    seg = text[end:end + 220]
    for sep in (".", "!", "?", "\n"):
        i = seg.find(sep)
        if i >= 0 and end + i < right:
            right = end + i
    return text[left:right]


def _sits_hits(text: str) -> list[re.Match]:
    """'cabin sits / home on the property / building in place' anchors."""
    hits = []
    for m in _PATTERN_SITS.finditer(text):
        if _ANCHOR_PRE_END_RE.search(_pre_window(text, m.start(), 25)):
            continue
        # "house pad/site/area/spot in place" is a cleared lot, not a
        # structure; "six other homes sit on this road" is neighbour copy; a
        # neighbouring home's address is not this parcel's.
        if re.search(r"\b(?:pads?|sites?|areas?|spots?)\b", m.group(1), re.I):
            continue
        if re.search(r"\b(?:other|others|several|many|few|more|numerous)\b",
                     _pre_window(text, m.start(), 25), re.I):
            continue
        win = text[max(0, m.start() - 90): m.end() + 40]
        if _CROSS_LISTING_RE.search(win):
            continue
        if re.match(r"improvement", text[m.start():m.start() + 12], re.I):
            # "improvements in place" is well/driveway/cleared-homesite
            # boilerplate; real structures ride the inclusion collector.
            continue
        if _REMOVAL_POST_RE.search(text[m.end():m.end() + 45]):
            continue  # "homes … move them or remove them"
        if _future_veto(text, m.start()):
            continue
        hits.append(m)
    return hits


def existing_asserted(text: str) -> bool:
    """True when any EXISTING evidence (simple patterns + checked windows) hits."""
    if any(p.search(text) for p in EXISTING_PATTERNS):
        return True
    if (_gap_pattern_hits(text) or _habitation_hits(text)
            or _asis_hits(text) or _manufactured_hits(text)
            or _spatial_hits(text) or _located_hits(text)
            or _bedroom_hits(text) or _sits_hits(text)
            or _interior_hits(text) or _inclusion_hits(text)
            or _remains_hits(text)):
        return True
    return False


def existing_evidence(text: str, limit: int = 3) -> list[str]:
    """Quotable ±60-char windows explaining an existing-structure verdict."""
    scan, _ = _neutralize_negations(text)
    spans: list[tuple[int, int]] = []
    for p in EXISTING_PATTERNS:
        m = p.search(scan)
        if m:
            spans.append((m.start(), m.end()))
    for coll in (_gap_pattern_hits(scan), _habitation_hits(scan),
                 _asis_hits(scan), _manufactured_hits(scan),
                 _spatial_hits(scan), _located_hits(scan),
                 _bedroom_hits(scan), _sits_hits(scan),
                 _interior_hits(scan), _inclusion_hits(scan),
                 _remains_hits(scan)):
        for m in coll[:limit]:
            spans.append((m.start(), m.end()))
    out: list[str] = []
    for start, end in spans:
        q = re.sub(r"\s+", " ",
                   text[max(0, start - 60): end + 60]).strip()
        if q and q not in out:
            out.append(q)
        if len(out) >= limit:
            break
    return out


def _token_in_future_context(text: str) -> bool:
    """True when every structure token sits inside future/vacant marketing copy.

    For each token occurrence, scan a window around it; if no window asserts
    presence (existing-pattern hit) but future patterns cover EVERY token
    window, the text is dream-home boilerplate, not evidence.
    """
    tokens = list(STRUCTURE_TOKEN_RE.finditer(text))
    if not tokens:
        # Nothing names a structure at all; STRONG vacant/dream copy still
        # settles the parcel ("blank slate — bring your dreams").
        return any(p.search(text) for p in _STRONG_FUTURE_RES)
    for m in tokens:
        window = text[max(0, m.start() - 90): m.end() + 90]
        if existing_asserted(window):
            return False
    covered = 0
    for m in tokens:
        window = text[max(0, m.start() - 90): m.end() + 90]
        if any(p.search(window) for p in FUTURE_PATTERNS) \
                or _DISMISS_RE.search(window):
            covered += 1
    return covered == len(tokens)


def description_verdict(text: Optional[str]) -> str:
    """HAS_BUILDING / NO_BUILDING / AMBIGUOUS from listing description text."""
    if not text or not text.strip():
        return AMBIGUOUS
    scan, negated = _neutralize_negations(text)
    if existing_asserted(scan):
        return HAS_BUILDING
    vacant = negated or bool(VACANT_RE.search(text))
    if vacant and not STRUCTURE_TOKEN_RE.search(scan):
        return NO_BUILDING
    if _token_in_future_context(scan):
        return NO_BUILDING
    return AMBIGUOUS


def insights_verdict(insights) -> bool:
    """True when Zillow homeInsight phrases name interior features."""
    for group in insights or []:
        for item in group.get("insights", []) if isinstance(group, dict) else []:
            for phrase in item.get("phrases", []) if isinstance(item, dict) else []:
                if isinstance(phrase, str) and INSIGHT_INTERIOR_RE.search(phrase):
                    return True
    return False


def _facts_year(prop: dict):
    year = (prop.get("resoFacts") or {}).get("yearBuilt") if isinstance(
        prop.get("resoFacts"), dict) else None
    return year if year not in (None, "", 0, "0", "Information pending") \
        else None


def facts_verdict(prop: dict) -> bool:
    """True when SSR facts imply a building (living area, year built, rooms)."""
    if not isinstance(prop, dict):
        return False
    for key in ("livingAreaValue", "livingArea"):
        value = prop.get(key)
        if isinstance(value, (int, float)) and value and float(value) > 0:
            return True
    if _facts_year(prop):
        return True
    for key in ("bedrooms", "bathrooms"):
        value = prop.get(key)
        if isinstance(value, (int, float)) and value and float(value) > 0:
            return True
    return False


def classify_listing(description: Optional[str], insights=None,
                     prop: Optional[dict] = None) -> str:
    """Overall text-side verdict for one listing's harvested detail payload."""
    if facts_verdict(prop or {}):
        return HAS_BUILDING
    desc = description_verdict(description)
    if desc == HAS_BUILDING:
        return HAS_BUILDING
    if insights_verdict(insights):
        return HAS_BUILDING
    if desc == NO_BUILDING:
        # Vacant text, no structure facts — but many photos still deserves a
        # look; the harvest layer decides via needs_images().
        return NO_BUILDING
    return AMBIGUOUS


def needs_images(verdict: str, photo_count: Optional[int]) -> bool:
    """Which listings warrant the (expensive) photo-building check.

    Ambiguous ones always; 'no_building' ones only when the photo count is
    suspiciously high for bare land (interior shots hide in big galleries).
    """
    if verdict == HAS_BUILDING:
        return False
    if verdict == AMBIGUOUS:
        return True
    return bool(photo_count and photo_count >= 6)
