"""County courthouse street addresses (sale-venue filter).

Trustee/mortgage sales are held AT the courthouse, so OCR notice text
routinely contains the courthouse address ("sell ... in front of the ...
Courthouse, 8095 Rutledge Pike") right next to the property description.
Address extractors must reject these or listings get the auction site
stored as the property (then GIS/TNMap enrich the wrong parcel).

Sources: nccourts.gov location pages (NC, fetched 2026-09-12), UT CTAS
county directory + circuit-court-clerks pages (TN), ACCG county pages
(GA). Counties whose directory entries are PO-Box-only are omitted (a
PO Box can never match a street address). SC/AL/KY need no registry:
no free-text extractor covers those states (bellcarrington is
structured sheet data).
"""
from __future__ import annotations
import re
from typing import Dict, Optional

COURTHOUSES: Dict[str, Dict[str, Dict[str, str]]] = {
    "nc": {
        "alleghany": {"address": "12 N Main St", "city": "Sparta"},
        "ashe": {"address": "150 Government Circle", "city": "Jefferson"},
        "avery": {"address": "200 Montezuma St", "city": "Newland"},
        "buncombe": {"address": "60 Court Plaza", "city": "Asheville"},
        "burke": {"address": "201 South Green St", "city": "Morganton"},
        "cherokee": {"address": "75 Peachtree St", "city": "Murphy"},
        "clay": {"address": "261 Courthouse Dr", "city": "Hayesville"},
        "graham": {"address": "12 Court St", "city": "Robbinsville"},
        "haywood": {"address": "285 N Main St", "city": "Waynesville"},
        "henderson": {"address": "200 N Grove St", "city": "Hendersonville"},
        "jackson": {"address": "401 Grindstaff Cove Rd", "city": "Sylva"},
        "macon": {"address": "5 W Main St", "city": "Franklin"},
        "madison": {"address": "258 Carolina Lane", "city": "Marshall"},
        "mcdowell": {"address": "21 S Main St", "city": "Marion"},
        "mitchell": {"address": "328 Longview Dr", "city": "Bakersville"},
        "polk": {"address": "One Courthouse Square", "city": "Columbus"},
        "swain": {"address": "101 Mitchell Street", "city": "Bryson City"},
        "transylvania": {"address": "7 East Main St", "city": "Brevard"},
        "watauga": {"address": "842 W King St", "city": "Boone"},
        "yancey": {"address": "110 Towne Square", "city": "Burnsville"},
    },
    "tn": {
        "anderson": {"address": "100 N. Main Street", "city": "Clinton"},
        "blount": {"address": "926 E Lamar Alexander Parkway", "city": "Maryville"},
        "carter": {"address": "900 E. Elk Ave", "city": "Elizabethton"},
        "claiborne": {"address": "415 Straight Creek Road", "city": "New Tazewell"},
        "cocke": {"address": "111 Court Avenue", "city": "Newport"},
        "coffee": {"address": "1329 McArthur Dr.", "city": "Manchester"},
        "cumberland": {"address": "60 Justice Center Drive", "city": "Crossville"},
        "fentress": {"address": "140 Justice Center Drive", "city": "Jamestown"},
        "grainger": {"address": "8095 Rutledge Pike", "city": "Rutledge"},
        "greene": {"address": "101 South Main Street", "city": "Greeneville"},
        "hamblen": {"address": "440 N. Jackson St.", "city": "Morristown"},
        "hamilton": {"address": "625 Georgia Avenue", "city": "Chattanooga"},
        "hancock": {"address": "1237 Main Street", "city": "Sneedville"},
        "hawkins": {"address": "115 Justice Center Dr", "city": "Rogersville"},
        "jefferson": {"address": "765 Justice Center Drive", "city": "Dandridge"},
        "johnson": {"address": "222 West Main Street", "city": "Mountain City"},
        "knox": {"address": "400 Main Street", "city": "Knoxville"},
        "mcminn": {"address": "1317 South White Street", "city": "Athens"},
        "monroe": {"address": "5400 New Highway 68", "city": "Madisonville"},
        "overton": {"address": "1000 JT Poindexter Drive", "city": "Livingston"},
        "pickett": {"address": "1 Courthouse Square", "city": "Byrdstown"},
        "roane": {"address": "200 East Race Street", "city": "Kingston"},
        "scott": {"address": "575 Scott High Drive", "city": "Huntsville"},
        "sequatchie": {"address": "351 Fredonia Road", "city": "Dunlap"},
        "sevier": {"address": "125 Court Avenue", "city": "Sevierville"},
        "sullivan": {"address": "3411 Highway 126", "city": "Blountville"},
        "union": {"address": "901 Main Street", "city": "Maynardville"},
        "van_buren": {"address": "179 Veterans Square", "city": "Spencer"},
        "warren": {"address": "201 Locust St.", "city": "McMinnville"},
        "washington": {"address": "108 West Jackson Blvd.", "city": "Jonesborough"},
        "white": {"address": "111 Depot Street", "city": "Sparta"},
    },
    "ga": {
        "dawson": {"address": "25 Justice Way", "city": "Dawsonville"},
        "fannin": {"address": "400 West Main Street", "city": "Blue Ridge"},
        "gilmer": {"address": "1 Broad Street", "city": "Ellijay"},
        "habersham": {"address": "130 Jacob's Way", "city": "Clarkesville"},
        "lumpkin": {"address": "99 Courthouse Hill", "city": "Dahlonega"},
        "pickens": {"address": "1266 East Church Street", "city": "Jasper"},
        "rabun": {"address": "25 Courthouse Square", "city": "Clayton"},
        "towns": {"address": "48 River Street", "city": "Hiawassee"},
        "union": {"address": "65 Courthouse Street", "city": "Blairsville"},
        "white": {"address": "1235 Helen Hwy", "city": "Cleveland"},
    },
}

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}

_SUFFIX_CANON = {
    "st": "street", "ave": "avenue", "av": "avenue", "dr": "drive",
    "rd": "road", "ln": "lane", "blvd": "boulevard", "cir": "circle",
    "ct": "court", "pl": "place", "pkwy": "parkway", "hwy": "highway",
    "sq": "square", "trl": "trail", "ter": "terrace", "ctr": "center",
}

_UNIT_RE = re.compile(
    r"\b(suite|ste|room|rm|floor|fl|unit|building|bldg|#)\b.*$", re.IGNORECASE)


def _split_number(addr: str) -> tuple[Optional[int], str]:
    """(house number or None, remaining street text)."""
    words = re.sub(r"[.,#]", " ", addr or "").split()
    if not words:
        return None, ""
    first = words[0].lower()
    if first.isdigit():
        return int(first), " ".join(words[1:])
    if first in _WORD_NUMS:
        return _WORD_NUMS[first], " ".join(words[1:])
    return None, " ".join(words)


def _canon_core(text: str) -> str:
    words = _UNIT_RE.sub("", text or "").lower().split()
    return " ".join(_SUFFIX_CANON.get(w, w) for w in words)


def courthouse_for(county: str, state: str) -> Optional[Dict[str, str]]:
    """Registry record for a county, or None (unknown / PO-only)."""
    return COURTHOUSES.get((state or "").strip().lower(), {}).get(
        (county or "").strip().lower())


def is_courthouse_address(address: str, county: str, state: str) -> bool:
    """True when *address* is the county's courthouse (sale venue).

    Conservative by design: house numbers must match when both sides have
    them (a legit property elsewhere on the same road is never rejected);
    numberless matches require full canonical equality.
    """
    rec = courthouse_for(county, state)
    if not rec or not (address or "").strip():
        return False
    a_num, a_core = _split_number(address)
    c_num, c_core = _split_number(rec["address"])
    if a_num is not None and c_num is not None:
        return a_num == c_num and _canon_core(a_core) == _canon_core(c_core)
    return _canon_core(f"{a_num or ''} {a_core}".strip()) == \
        _canon_core(f"{c_num or ''} {c_core}".strip())
