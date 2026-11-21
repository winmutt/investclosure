"""Standalone configuration for investclosure foreclosures.

All tunable parameters are read from environment variables with sensible defaults.
File-system paths are configurable via env vars so the same code can run
from any directory or container.

Usage:
    from scraper.config import config

    # Filter thresholds
    config.MIN_ACRES            # default 1.1
    config.MAX_PRICE            # default 0 (no cap for foreclosures)

    # File-system paths
    config.data_dir          # Path to data/
    config.db_path           # Path to SQLite DB
    config.backups_dir       # Path to backups/

    # Scraper settings (env-overridable)
    config.TWO_CAPTCHA_API_KEY
    config.PROXY_URL
    config.NCFORECLOSURES_BASE_URL
    config.TNFORECLOSURES_BASE_URL
"""
from __future__ import annotations

import os
import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _required(name: str) -> str:
    """Return env var value or exit with fatal message."""
    val = os.environ.get(name, "").strip()
    if not val:
        print(
            f"\n{'!' * 70}\n"
            f"  FATAL: Missing required environment variable: {name}\n"
            f"{'!' * 70}\n",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    return val


def _opt_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _opt_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(
            f"\n{'!' * 70}\n"
            f"  FATAL: Invalid value for {name}={raw!r} (expected float)\n"
            f"{'!' * 70}\n",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


def _opt_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"\n{'!' * 70}\n"
            f"  FATAL: Invalid value for {name}={raw!r} (expected int)\n"
            f"{'!' * 70}\n",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Site constants (env-overridable)
# ---------------------------------------------------------------------------

def _site_env(prefix: str, name: str, default: str) -> str:
    return _opt_str(f"{prefix}_{name.upper()}", default)


NCFORECLOSURES_BASE_URL = _site_env("NCFORECLOSURES", "BASE_URL", "https://www.ncnotices.com")
NCFORECLOSURES_CAPTCHA_SITE_KEY = _site_env(
    "NCFORECLOSURES", "CAPTCHA_SITE_KEY",
    "6LeRyw8sAAAAAJRiqeOxNIyHIZ0c4y6sL-qWbt63"
)

TNFORECLOSURES_BASE_URL = _site_env("TNFORECLOSURES", "BASE_URL", "https://www.tnpublicnotice.com")
TNFORECLOSURES_CAPTCHA_SITE_KEY = _site_env(
    "TNFORECLOSURES", "CAPTCHA_SITE_KEY",
    "6LdtSg8sAAAAADTdRyZxJ2R2sS82pKALNMvMqSyL"
)

NCFORECLOSURES_POPULAR_SEARCH_VALUE = _site_env(
    "NCFORECLOSURES", "POPULAR_SEARCH_VALUE",
    "Foreclosures"
)
NCNOTICES_TURNSTILE_SITE_KEY = _site_env(
    "NCNOTICES", "TURNSTILE_SITE_KEY",
    "0x4AAAAAADs-29tdUBxeI6cO"
)
TNFORECLOSURES_POPULAR_SEARCH_VALUE = _site_env(
    "TNFORECLOSURES", "POPULAR_SEARCH_VALUE",
    "Tax Sales"
)
# TN public notice detail pages are gated by a Cloudflare Turnstile that uses
# the SAME sitekey as ncnotices.com (shared Tennessee Press Association infra).
TNFORECLOSURES_TURNSTILE_SITE_KEY = _site_env(
    "TNFORECLOSURES", "TURNSTILE_SITE_KEY",
    "0x4AAAAAADs-29tdUBxeI6cO"
)

# GA public notices (Georgia Press Association) -- same ASP.NET WebForms
# platform as the TN site, but its own Turnstile sitekey.
GAFORECLOSURES_BASE_URL = _site_env("GAFORECLOSURES", "BASE_URL", "https://www.georgiapublicnotice.com")
GAFORECLOSURES_TURNSTILE_SITE_KEY = _site_env(
    "GAFORECLOSURES", "TURNSTILE_SITE_KEY",
    "0x4AAAAAADs-xpiU_yrMy8ZY"
)
GAFORECLOSURES_POPULAR_SEARCH_VALUE = _site_env(
    "GAFORECLOSURES", "POPULAR_SEARCH_VALUE",
    "Tax Sales"
)
# Popular-search categories to scrape. Defaults to tax sales plus the two
# categories that host mortgage / lender foreclosure "Sheriff's Sales" (which
# the original tax-sale-only scope deliberately excluded). Override with a
# comma-separated list via GAFORECLOSURES_CATEGORIES.
GAFORECLOSURES_CATEGORIES = _site_env(
    "GAFORECLOSURES", "CATEGORIES",
    "Tax Sales,Foreclosures,Sheriff's/Marshal's Sales"
)

# AL public notices (Alabama Press Association) -- alabamapublicnotices.com
# runs the SAME ASP.NET WebForms platform as TN/GA/NC (verified 2026-10-22:
# (S(session))/Search.aspx + hdnPKValue grid + Turnstile detail gate). Its
# Turnstile sitekey is identical to GA's (shared IP-way infrastructure).
ALFORECLOSURES_BASE_URL = _site_env("ALFORECLOSURES", "BASE_URL", "https://www.alabamapublicnotices.com")
ALFORECLOSURES_TURNSTILE_SITE_KEY = _site_env(
    "ALFORECLOSURES", "TURNSTILE_SITE_KEY",
    "0x4AAAAAADs-xpiU_yrMy8ZY"
)
# AL popular-search categories are selected by LABEL (option values are
# site-intern numeric ids: Foreclosures=3, Delinquent Taxes=33).
ALFORECLOSURES_CATEGORIES = _site_env(
    "ALFORECLOSURES", "CATEGORIES",
    "Foreclosures,Delinquent Taxes"
)

# KY public notices (Kentucky Press Association / Newz Group) --
# kypublicnotice.com is a DIFFERENT platform (plain CodeIgniter/PHP search
# form, no ASP.NET WebForms, no Turnstile): POST /index.php/main/search with
# counties=<Name>; notice pages are newspaper-page PDFs under KYLegals/.
KYFORECLOSURES_BASE_URL = _site_env("KYFORECLOSURES", "BASE_URL", "https://kypublicnotice.com")


# ---------------------------------------------------------------------------
# Full qualifying county lists
# Source: realestate/scraper/config.py QUALIFYING_COUNTIES
# Criteria (both enforced, not assumed):
#   * county PEAK elevation >= 1700ft — the parcel itself is then measured with
#     USGS 3DEP and reaped below config.MIN_ELEVATION (scraper/elevation.py)
#   * county ENTIRELY within config.RADIUS_MILES of the origin address — nearest
#     boundary point from USGS polygons, scraper/county_radius.json
# Pruned 2026-11-17 by the distance rule. Distances are the county's nearest
# point to the origin; a county returns if RADIUS_MILES is raised above it and
# `python3 -m scraper.backfill_radius` un-archives (or the rows are re-scraped).
# ---------------------------------------------------------------------------

# GA counties (11)
GA_FORECLOSURE_COUNTIES = [
    "dawson", "fannin", "gilmer", "habersham", "lumpkin",
    "murray", "pickens", "rabun", "towns", "union", "white",
]

# AL counties (5)
AL_FORECLOSURE_COUNTIES = [
    "blount", "cherokee", "cleburne", "dekalb", "talladega",
]

# KY counties (0) — retired 2026-11-17: every Kentucky county is outside the
# radius, so a KY row can never qualify. Former set with its nearest ground to
# the origin: whitley 180.1, bell 180.9, knox 187.2, harlan 191.6, perry 212.9.
# Whitley and Bell miss by less than a mile, so raising RADIUS_MILES to 181
# brings two of them back — that also means re-registering the scraper
# (see SCRAPER_MODULES in run.py) and re-adding the names here.
KY_FORECLOSURE_COUNTIES: list[str] = []

# NC counties (17) — was 25; removed 2026-11-17: alleghany 237.9, ashe 216.3,
# wilkes 212.9, watauga 203.1, catawba 195.1, avery 186.8, burke 182.8
NC_FORECLOSURE_COUNTIES = [
    "buncombe", "cherokee", "clay", "cleveland",
    "graham", "haywood", "henderson", "jackson",
    "macon", "madison", "mcdowell", "mitchell", "polk", "rutherford",
    "swain", "transylvania", "yancey",
]

# NC mountain counties (15) — the investclosure target set
# (same list Kania Law uses; excludes river-valley/urban counties).
# Removed 2026-11-17 by the distance rule: alleghany 237.9, ashe 216.3,
# watauga 203.1, avery 186.8, burke 182.8.
NC_MOUNTAIN_COUNTIES = [
    "buncombe", "cherokee", "clay", "graham", "haywood",
    "henderson", "jackson", "macon", "madison", "mcdowell",
    "mitchell", "polk", "swain", "transylvania", "yancey",
]

# SC counties (4)
SC_FORECLOSURE_COUNTIES = [
    "anderson", "greenville", "oconee", "pickens",
]

# TN counties (35) — mountain counties only.
# Removed 2026-11-17 by the distance rule: johnson 205.4, sullivan 191.8,
# carter 186.8.
TN_FORECLOSURE_COUNTIES = [
    "anderson", "bledsoe", "blount", "campbell", "claiborne",
    "cocke", "coffee", "cumberland", "fentress", "grainger", "greene",
    "grundy", "hamblen", "hamilton", "hancock", "hawkins", "jefferson",
    "knox", "marion", "mcminn", "monroe", "morgan", "overton",
    "pickett", "polk", "roane", "scott", "sequatchie", "sevier",
    "unicoi", "union", "van_buren", "warren", "washington", "white",
]

# GA mountain counties (7) — N GA high-country core, averaging well above
# ~1700ft (excludes lower foothill counties like dawson, habersham, murray,
# pickens). Used by the ga_publicnotice scraper, scoped to the "Tax Sales" category.
GA_MOUNTAIN_COUNTIES = [
    "fannin", "gilmer", "lumpkin", "rabun", "towns", "union", "white",
]

# All states with county lists
QUALIFYING_STATES = ["GA", "AL", "KY", "NC", "SC", "TN"]

QUALIFYING_COUNTIES: Dict[str, List[str]] = {
    "GA": GA_FORECLOSURE_COUNTIES,
    "AL": AL_FORECLOSURE_COUNTIES,
    "KY": KY_FORECLOSURE_COUNTIES,
    "NC": NC_FORECLOSURE_COUNTIES,
    "SC": SC_FORECLOSURE_COUNTIES,
    "TN": TN_FORECLOSURE_COUNTIES,
}

# All county names flattened (lowercase)
TARGET_COUNTIES: List[str] = [
    c for clist in QUALIFYING_COUNTIES.values() for c in clist
]

# =============================================================================
# Verticals (product lines above the per-state Tax/Mtg/Re category tabs)
# -----------------------------------------------------------------------------
# A *vertical* is a top-level product/brand with its own county scope and
# qualification rules. Today the app ships two active verticals:
#   mountains — mountain foreclosures (elevation >= 1700ft baked into the
#               county lists above, plus MIN_ACRES / MAX_ACRES / MAX_PRICE).
#   rentals   — GA tax-sales within RENT_RADIUS_MILES of downtown Atlanta:
#               inexpensive small homes rentable near RENT_TARGET_RENT/mo in
#               lower-middle-income small towns. Qualified by the RentCast
#               zip-market screen (scraper/rentcast.py + rent_market_scan.py),
#               NOT by elevation/acreage. Sources in RENTAL_SOURCES claim it.
# 'beach' is a registered STUB: the beach-front / lake-front vertical, scoped
# to water-frontage counties and qualified by a *minimum distance-to-water*
# instead of acreage/elevation. Its water county map + distance-to-water data
# source land in a later pass; for now it renders as a stub in the dashboard.
# =============================================================================

VERTICALS: Dict[str, Dict[str, str]] = {
    "mountains": {"label": "Mountains", "status": "active"},
    "beach": {"label": "Beach", "status": "stub"},
    "rentals": {"label": "Rentals", "status": "active"},
}

DEFAULT_VERTICAL = "mountains"

# Sources that belong to the rentals vertical (see property_vertical() in
# server.py). Rows from these sources are EXEMPT from the mountains parcel
# rules (elevation / MIN_ACRES / the office-radius reap) — a $60k rental
# house on a 0.4 ac town lot at 900 ft is exactly the rentals product.
RENTAL_SOURCES: set = {"ga_tax_rentals"}

# ---------------------------------------------------------------------------
# Rentals vertical rules (measured, not assumed)
#   * distance — RENT_RADIUS_MILES from RENT_ORIGIN (downtown Atlanta),
#     judged per county from USGS boundary geometry in
#     scraper/county_radius_atlanta.json (built with
#     `python3 -m scraper.fetch_county_radius --states GA --origin <lat,lon>
#     --out scraper/county_radius_atlanta.json`)
#   * rentability — RentCast /markets zip screen (scraper/rent_market_scan.py,
#     verdicts cached in scraper/rent_markets.json): a zip qualifies when its
#     small-home (2–3 bd) market rent sits in [RENT_TARGET_MIN_RENT,
#     RENT_TARGET_MAX_RENT] per month and houses sell for at most
#     RENT_MAX_MEDIAN_PRICE — the inexpensive-homes / lower-middle-income
#     small-town profile. The scrape-scope counties are those holding a
#     qualifying zip (see scraper/rent_scope.py).
# ---------------------------------------------------------------------------

# GA popular-search categories for the rentals scraper: tax sales ONLY (the
# product is delinquent-tax sheriff sales, not bank security-deed sales).
GA_RENTAL_CATEGORIES = _site_env("GA_RENTAL", "CATEGORIES", "Tax Sales")

# =============================================================================
# NC County GIS Parcel URLs for Kania Law enrichment
# =============================================================================

GIS_PARCEL_URLS: Dict[str, Dict[str, Any]] = {
    "Alleghany": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Alleghany_Cadastral/FeatureServer/0",
        "field_name": "PARCELID",
        "portal_type": "arcgis",
    },

    "Cherokee": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Cadastral/FeatureServer/0",
        "field_name": "NEWPIN",
        "portal_type": "arcgis",
    },
    "Haywood": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Haywood_Cadastral/FeatureServer/0",
        "field_name": "PARCELIDN",
        "portal_type": "arcgis",
    },
    "Henderson": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Henderson_Cadastral/FeatureServer/0",
        "field_name": "PARCELIDN",
        "portal_type": "arcgis",
    },
    "Madison": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Madison_Cadastral/FeatureServer/0",
        "field_name": "PARCELIDN",
        "portal_type": "arcgis",
    },
    "Transylvania": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Transylvania_Cadastral/FeatureServer/0",
        "field_name": "PARCELIDN",
        "portal_type": "arcgis",
    },
    "Jackson": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Jackson_Cadastral/FeatureServer/0",
        "field_name": "PARCELIDN",
        "portal_type": "arcgis",
    },
    "Clay": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Clay_Cadastral/FeatureServer/0",
        "field_name": "PARCELIDN",
        "portal_type": "arcgis",
    },
    "Graham": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Graham_Cadastral/FeatureServer/0",
        "field_name": "PARCELIDN",
        "portal_type": "arcgis",
    },
    "Swain": {
        "arcgis_url": "https://services6.arcgis.com/GYPQqV4e8e7G5hJz/ArcGIS/rest/services/Swain_Cadastral/FeatureServer/0",
        "field_name": "PARCELIDN",
        "portal_type": "arcgis",
    },
}


# ---------------------------------------------------------------------------
# Config class
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Configuration singleton — all tunable via environment variables."""

    # ---- required vars (fatal if missing) ----
    TWO_CAPTCHA_API_KEY: str

    # ---- optional service keys (stored for back-pocket use) ----
    GOOGLE_MAPS_API_KEY: str
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    TELEGRAM_ENABLED: bool
    APP_URL: str

    # ---- filter thresholds ----
    MIN_ACRES: float
    MAX_ACRES: float
    MAX_PRICE: int  # 0 = no cap (foreclosures typically have no price)
    MIN_ELEVATION: float  # per-parcel ground elevation, feet (0 = disabled)
    RADIUS_MILES: float   # distance from ORIGIN, statute miles (0 = disabled)
    ORIGIN_LAT: float     # the product's origin address
    ORIGIN_LON: float

    # ---- rentals vertical rules ----
    RENT_RADIUS_MILES: float     # from RENT_ORIGIN, statute miles (0 = disabled)
    RENT_ORIGIN_LAT: float       # downtown Atlanta (Five Points)
    RENT_ORIGIN_LON: float
    RENT_TARGET_MIN_RENT: float  # small-home market rent band, $/month
    RENT_TARGET_MAX_RENT: float
    RENT_MIN_RENT_LISTINGS: int  # minimum rental listings to trust a zip's rent stats
    RENT_MAX_MEDIAN_PRICE: int   # zip's median sale price ceiling (inexpensive homes)
    RENTCAST_API_KEY: str

    # ---- proxy ----
    PROXY_URL: Optional[str]

    # ---- scraper settings ----
    DELAY_RANGE: Tuple[float, float]
    CAPTCHA_ENABLED: bool
    PROXY_ENABLED: bool

    # ---- file-system paths (all configurable via env) ----
    data_dir: Path
    db_path: Path
    backups_dir: Path
    logs_dir: Path

    def __init__(
        self,
        data_dir: Optional[str] = None,
        db_path: Optional[str] = None,
        backups_dir: Optional[str] = None,
        logs_dir: Optional[str] = None,
    ) -> None:
        """
        Initialize config with paths resolved from env vars or defaults.

        Path resolution (highest priority first):
          1. Explicit constructor args
          2. Env vars: INVESTCLOSURE_DATA_DIR, INVESTCLOSURE_DB_PATH, etc.
          3. Defaults: relative to this file's parent (scraper/)
        """
        # Resolve base dir
        base = Path(__file__).resolve().parent.parent  # investclosure/

        # Paths with env var / constructor override
        self.data_dir = Path(
            data_dir or os.environ.get("INVESTCLOSURE_DATA_DIR", str(base / "data"))
        )
        self.db_path = Path(
            db_path or os.environ.get(
                "INVESTCLOSURE_DB_PATH", str(self.data_dir / "investclosure.db")
            )
        )
        self.backups_dir = Path(
            backups_dir or os.environ.get(
                "INVESTCLOSURE_BACKUPS_DIR", str(self.data_dir / "backups")
            )
        )
        self.logs_dir = Path(
            logs_dir or os.environ.get(
                "INVESTCLOSURE_LOGS_DIR", str(self.data_dir / "logs")
            )
        )

        # CAPTCHA key — optional at import, required when scrapers run (validated in base.py)
        self.TWO_CAPTCHA_API_KEY = _opt_str("TWO_CAPTCHA_API_KEY", "")

        # Google Maps API key — kept for back-pocket use (elevation/GMaps enrichment)
        # Not required for current scrapers but stored like realestate's config.py
        self.GOOGLE_MAPS_API_KEY = _opt_str("GOOGLE_MAPS_API_KEY", "")

        # Telegram notification keys — copied from realestate (podman-compose.yml)
        # TELEGRAM_ENABLED defaults false so import never crashes if unset
        self.TELEGRAM_BOT_TOKEN = _opt_str("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID = _opt_str("TELEGRAM_CHAT_ID", "")
        _telegram_enabled_raw = _opt_str("TELEGRAM_ENABLED", "false")
        self.TELEGRAM_ENABLED = _telegram_enabled_raw.lower() in ("true", "1", "yes")

        # Public app URL for links back to the dashboard (Telegram alerts)
        self.APP_URL = (
            _opt_str("INVESTCLOSURE_APP_URL", "http://winmutt.duckdns.org:5001")
            or "http://winmutt.duckdns.org:5001"
        ).rstrip("/")

        # Thresholds (env overridable)
        self.MIN_ACRES = _opt_float("INVESTCLOSURE_MIN_ACRES", 1.1)
        self.MAX_ACRES = _opt_float("INVESTCLOSURE_MAX_ACRES", 1000.0)
        self.MAX_PRICE = _opt_int("INVESTCLOSURE_MAX_PRICE", 0)  # no cap for foreclosures
        # Per-parcel ground elevation. The county lists only encode each
        # county's PEAK elevation (>=1700ft), so valley parcels inside a
        # qualifying county pass unless the parcel itself is measured. 0
        # disables the rule. Env: INVESTCLOSURE_MIN_ELEVATION.
        self.MIN_ELEVATION = _opt_float("INVESTCLOSURE_MIN_ELEVATION", 1700.0)

        # Distance half of the product rule: the parcel must also sit within
        # this many statute miles of ORIGIN. The qualifying county lists were
        # drawn from a 250-mile notion of "mountains within reach" and never
        # enforced; this is the enforced version. 0 disables the rule.
        # Env: INVESTCLOSURE_RADIUS_MILES.
        self.RADIUS_MILES = _opt_float("INVESTCLOSURE_RADIUS_MILES", 180.0)
        # 1690 Little Willeo Rd, Marietta GA 30068 — Census geocoder, exact
        # match. Env: INVESTCLOSURE_ORIGIN_LAT / _LON.
        self.ORIGIN_LAT = _opt_float("INVESTCLOSURE_ORIGIN_LAT",
                                     33.989302009439)
        self.ORIGIN_LON = _opt_float("INVESTCLOSURE_ORIGIN_LON",
                                     -84.401895017854)

        # Rentals vertical rules. Distance: RENT_RADIUS_MILES statute miles
        # from RENT_ORIGIN — downtown Atlanta, Five Points (the product is
        # "GA tax sales around Atlanta", deliberately a different origin from
        # the mountains office radius). 0 disables the distance screen.
        # Env: INVESTCLOSURE_RENT_RADIUS_MILES / _RENT_ORIGIN_LAT / _LON.
        self.RENT_RADIUS_MILES = _opt_float("INVESTCLOSURE_RENT_RADIUS_MILES",
                                            100.0)
        self.RENT_ORIGIN_LAT = _opt_float("INVESTCLOSURE_RENT_ORIGIN_LAT",
                                          33.7540)
        self.RENT_ORIGIN_LON = _opt_float("INVESTCLOSURE_RENT_ORIGIN_LON",
                                          -84.3905)
        # RentCast zip-market screen: small homes renting near $1,000/mo in
        # inexpensive markets. Env: INVESTCLOSURE_RENT_TARGET_MIN_RENT /
        # _MAX_RENT / _MIN_RENT_LISTINGS / _MAX_MEDIAN_PRICE.
        self.RENT_TARGET_MIN_RENT = _opt_float(
            "INVESTCLOSURE_RENT_TARGET_MIN_RENT", 800.0)
        self.RENT_TARGET_MAX_RENT = _opt_float(
            "INVESTCLOSURE_RENT_TARGET_MAX_RENT", 1200.0)
        self.RENT_MIN_RENT_LISTINGS = _opt_int(
            "INVESTCLOSURE_RENT_MIN_RENT_LISTINGS", 4)
        self.RENT_MAX_MEDIAN_PRICE = _opt_int(
            "INVESTCLOSURE_RENT_MAX_MEDIAN_PRICE", 250_000)
        # RentCast API key (rentcast.io) — header X-Api-Key. Env:
        # RENTCAST_API_KEY. Market scan caches per zip, so a monthly
        # refresh costs a few hundred calls.
        self.RENTCAST_API_KEY = _opt_str("RENTCAST_API_KEY", "")

        # Proxy (env overridable)
        proxy = _opt_str("INVESTCLOSURE_PROXY", "winmutt.com:8088")
        self.PROXY_URL = f"http://{proxy}" if proxy else None

        # Scraper defaults
        self.DELAY_RANGE = (1.5, 3.0)
        self.DELAY_RANGES = {
            "kania_law": (2.0, 4.0),
            "nc_publicnotice": (1.5, 3.0),
            "tn_publicnotice": (1.5, 3.0),
            "ga_publicnotice": (1.5, 3.0),
            "ga_tax_rentals": (1.5, 3.0),
            "al_publicnotice": (1.5, 3.0),
            "ky_publicnotice": (1.5, 3.0),
            "zls_nc": (2.0, 4.0),
            "newspaper_notices": (2.0, 4.0),
            "zillow": (2.5, 4.5),
            "default": (1.5, 3.0),
        }

        self.CAPTCHA_ENABLED = True
        self.PROXY_ENABLED = True

        # Per-scraper overrides (land-listing sources keep their own
        # economics; env overrides per scraper, e.g. ZILLOW_MIN_ACRES)
        self.SCRAPER_OVERRIDES: dict[str, dict[str, Any]] = {
            "zillow": {
                "min_acres": _opt_float("ZILLOW_MIN_ACRES", 5.0),
                "max_price": _opt_int("ZILLOW_MAX_PRICE", 250_000),
                "max_acres": _opt_float("ZILLOW_MAX_ACRES", 1000.0),
                # Parcel numbers need one rendered detail page each (~10s), so
                # a run only harvests this many rows that still lack one.
                "detail_limit": _opt_int("ZILLOW_DETAIL_LIMIT", 150),
            },
        }

        # Ensure directories exist
        for d in [self.data_dir, self.backups_dir, self.logs_dir]:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"WARNING: Cannot create directory {d}: {exc}", file=sys.stderr)

    def get_delay_range(self, name: str) -> tuple[float, float]:
        """Return (min, max) delay range for a scraper."""
        return self.DELAY_RANGES.get(name, self.DELAY_RANGES.get("default", (1.5, 3.0)))

    def get_min_acres(self, name: str) -> float:
        """Get minimum acres, with scraper override."""
        overrides = self.SCRAPER_OVERRIDES.get(name, {})
        return overrides.get("min_acres", self.MIN_ACRES)

    def get_max_price(self, name: str) -> int:
        """Get maximum price, with scraper override."""
        overrides = self.SCRAPER_OVERRIDES.get(name, {})
        return overrides.get("max_price", self.MAX_PRICE)

    def get_max_acres(self, name: str) -> float:
        """Get maximum acres, with scraper override (0 = no cap)."""
        overrides = self.SCRAPER_OVERRIDES.get(name, {})
        return overrides.get("max_acres", self.MAX_ACRES)

    def get_detail_limit(self, name: str, default: int = 0) -> int:
        """Max detail pages a scraper may render per run (0 = no cap)."""
        overrides = self.SCRAPER_OVERRIDES.get(name, {})
        return overrides.get("detail_limit", default)

    def should_use_proxy(self, name: str) -> bool:
        """Check if proxy should be used for this scraper."""
        return self.PROXY_ENABLED


# Singleton
config = Config()
