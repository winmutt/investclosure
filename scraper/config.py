"""Standalone configuration for investclosure foreclosures.

All tunable parameters are read from environment variables with sensible defaults.
File-system paths are configurable via env vars so the same code can run
from any directory or container.

Usage:
    from scraper.config import config

    # Filter thresholds
    config.MIN_ACRES            # default 10.0
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


# ---------------------------------------------------------------------------
# Full qualifying county lists (5-state scope)
# Source: realestate/scraper/config.py QUALIFYING_COUNTIES
# Criteria: within 250mi of Atlanta, peak elevation >= 1700ft
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

# KY counties (5)
KY_FORECLOSURE_COUNTIES = [
    "bell", "harlan", "knox", "perry", "whitley",
]

# NC counties (26)
NC_FORECLOSURE_COUNTIES = [
    "alleghany", "ashe", "avery", "buncombe", "burke",
    "caldwell", "catawba", "cherokee", "clay", "cleveland",
    "franklin", "graham", "haywood", "henderson", "jackson",
    "macon", "madison", "mcdowell", "mitchell", "polk", "rutherford",
    "swain", "transylvania", "watauga", "wilkes", "yancey",
]

# SC counties (4)
SC_FORECLOSURE_COUNTIES = [
    "anderson", "greenville", "oconee", "pickens",
]

# TN counties (37) — mountain counties only
TN_FORECLOSURE_COUNTIES = [
    "anderson", "bledsoe", "blount", "campbell", "carter", "claiborne",
    "cocke", "coffee", "cumberland", "fentress", "grainger", "greene",
    "grundy", "hamblen", "hamilton", "hancock", "hawkins", "jefferson",
    "johnson", "knox", "marion", "mcminn", "monroe", "morgan", "overton",
    "pickett", "polk", "roane", "scott", "sequatchie", "sevier",
    "sullivan", "unico", "union", "van_buren", "warren", "washington", "white",
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

    # ---- filter thresholds ----
    MIN_ACRES: float
    MAX_ACRES: float
    MAX_PRICE: int  # 0 = no cap (foreclosures typically have no price)

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

        # Thresholds (env overridable)
        self.MIN_ACRES = _opt_float("INVESTCLOSURE_MIN_ACRES", 10.0)
        self.MAX_ACRES = _opt_float("INVESTCLOSURE_MAX_ACRES", 1000.0)
        self.MAX_PRICE = _opt_int("INVESTCLOSURE_MAX_PRICE", 0)  # no cap for foreclosures

        # Proxy (env overridable)
        proxy = _opt_str("INVESTCLOSURE_PROXY", "winmutt.com:8088")
        self.PROXY_URL = f"http://{proxy}" if proxy else None

        # Scraper defaults
        self.DELAY_RANGE = (1.5, 3.0)
        self.DELAY_RANGES = {
            "kania_law": (2.0, 4.0),
            "ncforeclosures": (1.5, 3.0),
            "tnforeclosures": (1.5, 3.0),
            "zls_nc": (2.0, 4.0),
            "hutchens_law": (2.0, 4.0),
            "newspaper_notices": (2.0, 4.0),
            "default": (1.5, 3.0),
        }

        self.CAPTCHA_ENABLED = True
        self.PROXY_ENABLED = True

        # Per-scraper overrides
        self.SCRAPER_OVERRIDES: dict[str, dict[str, Any]] = {}

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

    def should_use_proxy(self, name: str) -> bool:
        """Check if proxy should be used for this scraper."""
        return self.PROXY_ENABLED


# Singleton
config = Config()
