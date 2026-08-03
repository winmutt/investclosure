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
    source            TEXT,
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
    parcel_number     TEXT,
    deed_book         TEXT,
    first_seen        TEXT,
    last_seen         TEXT,
    seen_count        INTEGER DEFAULT 1,
    dedup_hash        TEXT,
    status            TEXT DEFAULT 'active',
    tags              TEXT,
    notes             TEXT,
    scraped_at        TEXT,
    google_maps_url   TEXT,
    google_maps_topo_url TEXT,
    gis_url           TEXT,
    elevation_ft      REAL,
    parcel_screenshot TEXT,
    manual_acres_set TEXT,
    manual_acres_override REAL
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

    # Incremental schema migrations
    _apply_migrations(conn)

    conn.row_factory = sqlite3.Row
    return conn


_SCHEMAS_VERSION = 2


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add new columns to properties table if missing."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(properties)").fetchall()}

    migrations = [
        ("deed_book", "ALTER TABLE properties ADD COLUMN deed_book TEXT"),
        ("manual_acres_set", "ALTER TABLE properties ADD COLUMN manual_acres_set TEXT"),
        ("manual_acres_override", "ALTER TABLE properties ADD COLUMN manual_acres_override REAL"),
    ]
    for col, sql in migrations:
        if col not in existing:
            try:
                conn.execute(sql)
                conn.commit()
                logger.info("Migration: added column %s to properties", col)
            except Exception as e:
                logger.warning("Migration failed (column may exist): %s", e)


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
    source: Optional[str],
    source_listing_id: Optional[str],
    url: Optional[str],
    address: Optional[str],
    city: Optional[str],
    county: Optional[str],
    state: Optional[str],
    zip_code: Optional[str],
    latitude: Optional[float],
    longitude: Optional[float],
    price_cents: Optional[int],
    acres: Optional[float] = None,
    description: Optional[str] = None,
    property_type: Optional[str] = "foreclosure",
    listing_date: Optional[str] = None,
    auction_date: Optional[str] = None,
    close_date: Optional[str] = None,
    upset_bid: Optional[str] = None,
    foreclosure_key: Optional[str] = None,
    parcel_number: Optional[str] = None,
    deed_book: Optional[str] = None,
    first_seen: Optional[str] = None,
    last_seen: Optional[str] = None,
    google_maps_url: Optional[str] = None,
    google_maps_topo_url: Optional[str] = None,
    gis_url: Optional[str] = None,
    elevation_ft: Optional[float] = None,
    parcel_screenshot: Optional[str] = None,
) -> Tuple[str, sqlite3.Row]:
    """Insert or update a property row.

    If the existing row has ``manual_acres_set`` populated, the acres
    column is NOT overwritten — it is considered manually locked.
    """
    dedup_hash = compute_dedup_hash(
        address or "", city or "", county or "", state or "",
        zip_code or "", latitude, longitude
    )

    today = date.today().isoformat()
    first_seen_val = first_seen or today
    last_seen_val = last_seen or today

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
        # For Kania Law source, detect foreclosure detail changes.
        # If the foreclosure_key differs, treat as a new record.
        if source == "kania_law" and foreclosure_key is not None:
            existing_key = (existing["foreclosure_key"] or "")
            new_key = foreclosure_key
            if existing_key != new_key:
                logger.debug(
                    "Property %s: foreclosure_key changed from '%s' to '%s' — inserting as new",
                    source_listing_id or existing["source_listing_id"],
                    existing_key,
                    new_key,
                )
                cur = conn.execute(
                    """INSERT INTO properties
                       (source, source_listing_id, url, address, city, county, state, zip_code,
                        latitude, longitude, price_cents, acres, description, property_type,
                        listing_date, auction_date, close_date, upset_bid, foreclosure_key,
                        parcel_number, google_maps_url, google_maps_topo_url, gis_url, elevation_ft, parcel_screenshot,
                        first_seen, last_seen, seen_count, dedup_hash, status)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        source, source_listing_id, url, address, city, county, state, zip_code,
                        latitude, longitude, price_cents, acres, description, property_type,
                        listing_date, auction_date, close_date, upset_bid, foreclosure_key,
                        parcel_number, deed_book, google_maps_url, google_maps_topo_url, gis_url, elevation_ft, parcel_screenshot,
                        first_seen_val, last_seen_val, 1, dedup_hash, "active",
                    ),
                )
                conn.commit()
                new_row = conn.execute(
                    "SELECT * FROM properties WHERE id=?", (cur.lastrowid,)
                ).fetchone()
                return "new", new_row

        updates = [
            "last_seen = ?",
            "seen_count = seen_count + 1",
        ]
        values: list[Any] = [
            last_seen_val,
        ]
        # Only overwrite acres if manual override is not set
        manual_set = existing["manual_acres_set"]
        if not (manual_set or "").strip():
            updates.append("acres = ?")
            values.append(acres)
        updates.extend([
            "price_cents = ?",
            "description = ?",
        ])
        values.extend([
            price_cents, description,
        ])
        # Only update parcel_number if existing record doesn't already have one
        if not existing["parcel_number"] and parcel_number:
            updates.append("parcel_number = ?")
            values.append(parcel_number)
        updates.extend([
            "auction_date = ?",
            "close_date = ?",
            "upset_bid = ?",
            "foreclosure_key = ?",
            "deed_book = ?",
            "google_maps_url = ?",
            "google_maps_topo_url = ?",
            "gis_url = ?",
            "elevation_ft = ?",
            "parcel_screenshot = ?",
        ])
        values.extend([
            auction_date, close_date, upset_bid, foreclosure_key,
            deed_book, google_maps_url, google_maps_topo_url, gis_url, elevation_ft, parcel_screenshot,
        ])
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
    cur = conn.execute(
        """INSERT INTO properties
           (source, source_listing_id, url, address, city, county, state, zip_code,
            latitude, longitude, price_cents, acres, description, property_type,
            listing_date, auction_date, close_date, upset_bid, foreclosure_key,
            parcel_number, deed_book, google_maps_url, google_maps_topo_url, gis_url, elevation_ft, parcel_screenshot,
            first_seen, last_seen, seen_count, dedup_hash, status)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source, source_listing_id, url, address, city, county, state, zip_code,
            latitude, longitude, price_cents, acres, description, property_type,
            listing_date, auction_date, close_date, upset_bid, foreclosure_key,
            parcel_number, deed_book, google_maps_url, google_maps_topo_url, gis_url, elevation_ft, parcel_screenshot,
            first_seen_val, last_seen_val, 1, dedup_hash, "active",
        ),
    )
    conn.commit()
    new_row = conn.execute(
        "SELECT * FROM properties WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    return "new", new_row


def insert_property(
    conn: sqlite3.Connection,
    source: Optional[str],
    source_listing_id: Optional[str],
    url: Optional[str],
    address: Optional[str],
    city: Optional[str],
    county: Optional[str],
    state: Optional[str],
    zip_code: Optional[str],
    latitude: Optional[float],
    longitude: Optional[float],
    price_cents: Optional[int],
    acres: Optional[float] = None,
    description: Optional[str] = None,
    property_type: Optional[str] = "foreclosure",
    listing_date: Optional[str] = None,
    auction_date: Optional[str] = None,
    close_date: Optional[str] = None,
    upset_bid: Optional[str] = None,
    foreclosure_key: Optional[str] = None,
    parcel_number: Optional[str] = None,
    deed_book: Optional[str] = None,
    first_seen: Optional[str] = None,
    last_seen: Optional[str] = None,
    google_maps_url: Optional[str] = None,
    google_maps_topo_url: Optional[str] = None,
    gis_url: Optional[str] = None,
    elevation_ft: Optional[float] = None,
    parcel_screenshot: Optional[str] = None,
) -> Tuple[str, sqlite3.Row]:
    """Insert or update a property record."""
    return _upsert_property(
        conn, source, source_listing_id, url, address, city, county, state,
        zip_code, latitude, longitude, price_cents, acres,
        description=description, property_type=property_type,
        listing_date=listing_date, auction_date=auction_date,
        close_date=close_date, upset_bid=upset_bid,
        foreclosure_key=foreclosure_key, parcel_number=parcel_number,
        deed_book=deed_book,
        first_seen=first_seen, last_seen=last_seen,
        google_maps_url=google_maps_url, google_maps_topo_url=google_maps_topo_url,
        gis_url=gis_url, elevation_ft=elevation_ft, parcel_screenshot=parcel_screenshot,
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
    include_sources: Optional[list[str]] = None,
) -> int:
    """Archive properties where acres < min_acres.
    
    Args:
        source: Single source to filter by (legacy)
        include_sources: List of sources to archive (new, takes precedence)
    """
    source_filter = ""
    params: list[Any] = ["archived"]
    params.append(date.today().isoformat())  
    params.append(min_acres)
    
    if include_sources:
        placeholders = ",".join(["?"] * len(include_sources))
        source_filter = f" AND source IN ({placeholders})"
        params.extend(include_sources)
    elif source:
        source_filter = " AND source = ?"
        params.append(source)

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
