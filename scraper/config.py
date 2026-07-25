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
# Default qualifying counties (NC + TN mountain counties)
# ---------------------------------------------------------------------------

# NC counties (21 targeted by NC Foreclosures scraper)
NC_FORECLOSURE_COUNTIES = [
    "alleghany", "ashe", "avery", "buncombe", "burke",
    "caldwell", "catawba", "cherokee", "clay", "graham",
    "haywood", "henderson", "jackson", "madison", "mcdowell",
    "mitchell", "swain", "transylvania", "watauga", "wilkes", "yancey",
]

# TN counties (indexed by tnforeclosures county checkbox indices)
TN_FORECLOSURE_COUNTIES = [
    "anderson", "bedford", "benton", "bledsoe", "blount",
    "bradley", "campbell", "cannon", "carroll", "carter",
    "cheatham", "chester", "claiborne", "clay", "cocke",
    "coffee", "crockett", "cumberland", "davidson", "decatur",
    "dekalb", "dickson", "dyer", "fayette", "fentress",
    "franklin", "gibson", "giles", "grainger", "greene",
    "grundy", "hamblen", "hamilton", "hancock", "hardeman",
    "hardin", "hawkins", "haywood", "henderson", "henry",
    "hickman", "houston", "humphreys", "jackson", "jefferson",
    "johnson", "knox", "lake", "lauderdale", "lawrence",
    "lewis", "lincoln", "loudon", "macon", "madison",
    "marion", "marshall", "maury", "mcminn", "mcnairy",
    "meigs", "monroe", "montgomery", "moore", "morgan",
    "obion", "overton", "perry", "pickett", "polk",
    "putnam", "rhea", "roane", "robertson", "rutherford",
    "scott", "sequatchie", "sevier", "shelby", "smith",
    "stewart", "sullivan", "sumner", "tipton", "trousdale",
    "unicoi", "union", "vanburen", "warren", "washington",
    "wayne", "weakley", "white", "williamson", "wilson",
]

# TN county checkbox indices (0-based) for TNForeclosureScraper
TNFORECLOSURES_COUNTY_INDICES: Dict[str, int] = {
    "anderson": 0, "bedford": 1, "benton": 2, "bledsoe": 3, "blount": 4,
    "bradley": 5, "campbell": 6, "cannon": 7, "carroll": 8, "carter": 9,
    "cheatham": 10, "chester": 11, "claiborne": 12, "clay": 13, "cocke": 14,
    "coffee": 15, "crockett": 16, "cumberland": 17, "davidson": 18, "decatur": 19,
    "dekalb": 20, "dickson": 21, "dyer": 22, "fayette": 23, "fentress": 24,
    "franklin": 25, "gibson": 26, "giles": 27, "grainger": 28, "greene": 29,
    "grundy": 30, "hamblen": 31, "hamilton": 32, "hancock": 33, "hardeman": 34,
    "hardin": 35, "hawkins": 36, "haywood": 37, "henderson": 38, "henry": 39,
    "hickman": 40, "houston": 41, "humphreys": 42, "jackson": 43, "jefferson": 44,
    "johnson": 45, "knox": 46, "lake": 47, "lauderdale": 48, "lawrence": 49,
    "lewis": 50, "lincoln": 51, "loudon": 52, "macon": 53, "madison": 54,
    "marion": 55, "marshall": 56, "maury": 57, "mcminn": 58, "mcnairy": 59,
    "meigs": 60, "monroe": 61, "montgomery": 62, "moore": 63, "morgan": 64,
    "obion": 65, "overton": 66, "perry": 67, "pickett": 68, "polk": 69,
    "putnam": 70, "rhea": 71, "roane": 72, "robertson": 73, "rutherford": 74,
    "scott": 75, "sequatchie": 76, "sevier": 77, "shelby": 78, "smith": 79,
    "stewart": 80, "sullivan": 81, "sumner": 82, "tipton": 83, "trousdale": 84,
    "unicoi": 85, "union": 86, "vanburen": 87, "warren": 88, "washington": 89,
    "wayne": 90, "weakley": 91, "white": 92, "williamson": 93, "wilson": 94,
}

# Popular search dropdown values (used by both ASP.NET scrapers)
NCFORECLOSURES_POPULAR_SEARCH_VALUE = "6"
TNFORECLOSURES_POPULAR_SEARCH_VALUE = "4"


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
        proxy = _opt_str("INVESTCLOSURE_PROXY", "")
        self.PROXY_URL = f"http://{proxy}" if proxy else None

        # Scraper defaults
        self.DELAY_RANGE = (1.5, 3.0)
        self.CAPTCHA_ENABLED = True
        self.PROXY_ENABLED = True

        # Ensure directories exist
        for d in [self.data_dir, self.backups_dir, self.logs_dir]:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"WARNING: Cannot create directory {d}: {exc}", file=sys.stderr)


# Singleton
config = Config()
