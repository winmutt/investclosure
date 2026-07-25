"""SQLite database for foreclosure properties.

All paths configurable via env vars or passed explicitly:
  INVESTCLOSURE_DB_PATH   — SQLite DB path (default: ./data/investclosure.db)
  INVESTCLOSURE_DATA_DIR  — Base data directory (default: ./data/)
"""
from __future__ import annotations
import sqlite3
import hashlib
import logging
from datetime import date
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from .config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL,
    source_listing_id TEXT,
    url               TEXT,
    address           TEXT,
    city              TEXT,
    county            TEXT,
    state             TEXT,
    zip_code          TEXT,
    latitude          REAL,
    longitude         REAL,
    price_cents       INTEGER,
    acres             REAL,
    description       TEXT,
    property_type     TEXT DEFAULT 'foreclosure',
    listing_date      TEXT,
    auction_date      TEXT,
    close_date        TEXT,
    upset_bid         TEXT,
    foreclosure_key   TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    seen_count        INTEGER DEFAULT 1,
    dedup_hash        TEXT,
    status            TEXT DEFAULT 'active',
    tags              TEXT,
    notes             TEXT,
    scraped_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_properties_source ON properties(source);
CREATE INDEX IF NOT EXISTS idx_properties_county_state ON properties(county, state);
CREATE INDEX IF NOT EXISTS idx_properties_status ON properties(status);
CREATE INDEX IF NOT EXISTS idx_properties_dedup_hash ON properties(dedup_hash);
CREATE INDEX IF NOT EXISTS idx_properties_first_seen ON properties(first_seen);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source             TEXT NOT NULL,
    started_at         TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT,
    properties_found   INTEGER DEFAULT 0,
    properties_new     INTEGER DEFAULT 0,
    properties_duplicate INTEGER DEFAULT 0,
    properties_updated INTEGER DEFAULT 0,
    status             TEXT DEFAULT 'running',
    error_message      TEXT
);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _ensure_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (or create) the SQLite DB and apply schema."""
    path = db_path or config.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Dedup hash
# ---------------------------------------------------------------------------

def compute_dedup_hash(
    address: str,
    city: str,
    county: str,
    state: str,
    zip_code: str,
    latitude: Optional[float],
    longitude: Optional[float],
) -> str:
    key = "|".join([
        (address or "").strip().lower(),
        (city or "").strip().lower(),
        (county or "").strip().lower(),
        (state or "").strip().lower(),
        (zip_code or "").strip(),
    ])
    if latitude is not None and longitude is not None:
        key += f"|{float(latitude):.6f},{float(longitude):.6f}"
    return hashlib.sha256(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Insert / Update / Query helpers
# ---------------------------------------------------------------------------

def _upsert_property(
    conn: sqlite3.Connection,
    source: str,
    source_listing_id: Optional[str],
    url: Optional[str],
    address: Optional[str],
    city: Optional[str],
    county: Optional[str],
    state: Optional[str],
    zip_code: Optional[str],
    latitude: Optional[float],
    longitude: Optional[float],
    price_cents: int,
    acres: float,
    description: Optional[str] = None,
    property_type: Optional[str] = "foreclosure",
    listing_date: Optional[str] = None,
    auction_date: Optional[str] = None,
    close_date: Optional[str] = None,
    upset_bid: Optional[str] = None,
    foreclosure_key: Optional[str] = None,
) -> Tuple[str, sqlite3.Row]:
    """
    Insert or update a property row.
    Returns ("new" | "duplicate", row).
    """
    dedup_hash = compute_dedup_hash(
        address or "", city or "", county or "", state or "",
        zip_code or "", latitude, longitude
    )

    # Check for exact source + listing_id match first
    existing = None
    if source and source_listing_id:
        existing = conn.execute(
            "SELECT * FROM properties WHERE source=? AND source_listing_id=? LIMIT 1",
            (source, source_listing_id),
        ).fetchone()

    # Fall back to dedup hash
    if not existing:
        existing = conn.execute(
            "SELECT * FROM properties WHERE dedup_hash=? LIMIT 1", (dedup_hash,)
        ).fetchone()

    if existing:
        updates = [
            "last_seen = ?",
            "seen_count = seen_count + 1",
            "acres = ?",
            "price_cents = ?",
            "description = ?",
            "auction_date = ?",
            "close_date = ?",
            "upset_bid = ?",
            "foreclosure_key = ?",
        ]
        values: list[Any] = [
            date.today().isoformat(),
            acres, price_cents, description,
            auction_date, close_date, upset_bid, foreclosure_key,
        ]
        values.append(existing["id"])
        conn.execute(
            f"UPDATE properties SET {', '.join(updates)} WHERE id=?",
            values,
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM properties WHERE id=?", (existing["id"],)
        ).fetchone()
        return "duplicate", updated

    # New property
    today = date.today().isoformat()
    cur = conn.execute(
        """INSERT INTO properties
           (source, source_listing_id, url, address, city, county, state, zip_code,
            latitude, longitude, price_cents, acres, description, property_type,
            listing_date, auction_date, close_date, upset_bid, foreclosure_key,
            first_seen, last_seen, seen_count, dedup_hash, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source, source_listing_id, url, address, city, county, state, zip_code,
            latitude, longitude, price_cents, acres, description, property_type,
            listing_date, auction_date, close_date, upset_bid, foreclosure_key,
            today, today, 1, dedup_hash, "active",
        ),
    )
    conn.commit()
    new_row = conn.execute(
        "SELECT * FROM properties WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return "new", new_row


def insert_property(
    conn: sqlite3.Connection,
    source: str,
    source_listing_id: Optional[str],
    url: Optional[str],
    address: Optional[str],
    city: Optional[str],
    county: Optional[str],
    state: Optional[str],
    zip_code: Optional[str],
    latitude: Optional[float],
    longitude: Optional[float],
    price_cents: int,
    acres: float,
    description: Optional[str] = None,
    property_type: Optional[str] = "foreclosure",
    listing_date: Optional[str] = None,
    auction_date: Optional[str] = None,
    close_date: Optional[str] = None,
    upset_bid: Optional[str] = None,
    foreclosure_key: Optional[str] = None,
) -> Tuple[str, sqlite3.Row]:
    """Insert or update a property record."""
    return _upsert_property(
        conn, source, source_listing_id, url, address, city, county, state,
        zip_code, latitude, longitude, price_cents, acres,
        description=description, property_type=property_type,
        listing_date=listing_date, auction_date=auction_date,
        close_date=close_date, upset_bid=upset_bid,
        foreclosure_key=foreclosure_key,
    )


def start_scrape_run(conn: sqlite3.Connection, source: str) -> int:
    """Start a scrape run record. Returns run id."""
    cur = conn.execute(
        "INSERT INTO scrape_runs (source) VALUES (?)", (source,)
    )
    conn.commit()
    return cur.lastrowid


def update_scrape_run(
    conn: sqlite3.Connection,
    run_id: int,
    found: int,
    new_count: int,
    duplicate_count: int,
    updated_count: int,
    status: str = "completed",
    error_message: Optional[str] = None,
) -> None:
    """Update scrape run record with final stats."""
    conn.execute(
        """UPDATE scrape_runs
           SET finished_at = datetime('now'),
               properties_found = ?,
               properties_new = ?,
               properties_duplicate = ?,
               properties_updated = ?,
               status = ?,
               error_message = ?
           WHERE id = ?""",
        (found, new_count, duplicate_count, updated_count, status, error_message, run_id),
    )
    conn.commit()


def get_new_since(
    conn: sqlite3.Connection,
    since_date: str,
    source: Optional[str] = None,
    limit: int = 100,
) -> List[sqlite3.Row]:
    """Return properties first_seen on or after since_date."""
    if source:
        return conn.execute(
            "SELECT * FROM properties WHERE first_seen >= ? AND source = ? AND status = 'active' ORDER BY first_seen DESC LIMIT ?",
            (since_date, source, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM properties WHERE first_seen >= ? AND status = 'active' ORDER BY first_seen DESC LIMIT ?",
        (since_date, limit),
    ).fetchall()


def get_all_active(
    conn: sqlite3.Connection,
    limit: int = 100,
    source: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Return active properties."""
    if source:
        return conn.execute(
            "SELECT * FROM properties WHERE status = 'active' AND source = ? ORDER BY first_seen DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM properties WHERE status = 'active' ORDER BY first_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()


def archive_below_acres(
    conn: sqlite3.Connection,
    min_acres: float,
    source: Optional[str] = None,
) -> int:
    """Archive properties where acres < min_acres."""
    source_filter = ""
    params: list[Any] = [min_acres]
    if source:
        source_filter = " AND source = ?"
        params.append(source)
    params.append("archived")
    params.append(date.today().isoformat())

    conn.execute(
        f"""\
        UPDATE properties
        SET status = ?, last_seen = ?
        WHERE status = 'active' AND acres > 0 AND acres < ?{source_filter}\
        """,
        params,
    )
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0]


def get_stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Return aggregate DB stats."""
    today = date.today().isoformat()
    stats: Dict[str, Any] = {}
    stats["total_active"] = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE status='active'"
    ).fetchone()[0]
    stats["total_seen"] = conn.execute(
        "SELECT COUNT(*) FROM properties"
    ).fetchone()[0]
    stats["today_new"] = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE first_seen = ?", (today,)
    ).fetchone()[0]
    stats["total_duplicates_seen"] = conn.execute(
        "SELECT COALESCE(SUM(seen_count - 1), 0) FROM properties"
    ).fetchone()[0]
    stats["total_archived"] = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE status='archived'"
    ).fetchone()[0]
    stats["scrape_runs"] = conn.execute(
        "SELECT COUNT(*) FROM scrape_runs"
    ).fetchone()[0]

    by_source = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM properties WHERE status='active' GROUP BY source ORDER BY cnt DESC"
    ).fetchall()
    stats["by_source"] = [(dict(r)["source"], dict(r)["cnt"]) for r in by_source]

    by_county = conn.execute(
        "SELECT county, state, COUNT(*) as cnt FROM properties WHERE status='active' AND county IS NOT NULL GROUP BY county, state ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    stats["by_county"] = [
        (f'{dict(r)["county"]}, {dict(r)["state"]}', dict(r)["cnt"]) for r in by_county
    ]
    return stats
