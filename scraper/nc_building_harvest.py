"""Harvest Zillow detail pages for ACTIVE NC rows with no bed/bath facts.

Purpose: find the mountain parcels that carry an EXISTING (often run-down)
structure while Zillow renders them as bare ``LOT`` listings — the case where
bedrooms/bathrooms are NULL but the description or the photos prove a building
(see :mod:`scraper.building_detect`).

Each detail page costs a fresh Camoufox profile (PerimeterX walls a session's
second same-origin navigation), so progress is checkpointed to
``scraper/tmp/nc_building_harvest.jsonl`` and interrupted runs resume by row
id. While a scheduled zillow sweep holds a ``scrape_runs`` row open the pass
PAUSES, so the two never hit Zillow concurrently from one IP.

Photos: only rows the text side cannot resolve (ambiguous, or "vacant" text
with a suspiciously large gallery) download their first few images to
``scraper/tmp/nc_building_photos/`` for the vision pass.

Usage (inside the container)::

    python3 -m scraper.nc_building_harvest --limit 10   # smoke test
    python3 -m scraper.nc_building_harvest              # full pass (hours)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import time
from typing import Optional

from .base import camoufox_context
from .building_detect import (classify_listing, existing_evidence,
                              needs_images)
from .db import _ensure_db
from .zillow import _NEXT_DATA_RE

logger = logging.getLogger(__name__)

MAX_PHOTOS_PER_LISTING = int(os.environ.get("NC_BIMG_MAX_PHOTOS", "6"))
MAX_IMAGE_SETS = int(os.environ.get("NC_BIMG_MAX", "500"))
PAGE_TIMEOUT_MS = 60000
DELAY_BETWEEN_DETAILS = (1.5, 3.0)

HARVEST_JSONL = os.path.join(os.path.dirname(__file__), "tmp",
                             "nc_building_harvest.jsonl")
PHOTOS_DIR = os.path.join(os.path.dirname(__file__), "tmp",
                          "nc_building_photos")


# ---------------------------------------------------------------------------
# Detail payload extraction (SSR __NEXT_DATA__ -> rich record)
# ---------------------------------------------------------------------------

def _photo_url(sources: list) -> Optional[str]:
    """Pick a ~800px JPEG from a mixedSources.jpeg list (smallest >=640px)."""
    best = None
    for src in sources or []:
        url, width = src.get("url"), src.get("width") or 0
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        if 640 <= width <= 1024:
            return url
        if best is None:
            best = url
    return best


def extract_rich(html: str) -> Optional[dict]:
    """Pull the building-relevant fields from a homedetails SSR payload.

    Returns None when the page carries no ``__NEXT_DATA__``/gdpClientCache
    (bot wall or removed listing); otherwise a dict of harvested fields.
    """
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
        cache_str = (data.get("props", {}).get("pageProps", {})
                     .get("componentProps", {}).get("gdpClientCache", ""))
        if not cache_str:
            return None
        cache = json.loads(cache_str)
    except (KeyError, json.JSONDecodeError, TypeError, AttributeError):
        return None
    for value in cache.values():
        if not (isinstance(value, dict) and "property" in value):
            continue
        prop = value["property"]
        if not isinstance(prop, dict):
            continue
        desc = prop.get("description")
        if isinstance(desc, dict):
            desc = desc.get("description")
        photos = []
        for ph in (prop.get("originalPhotos") or prop.get("responsivePhotos") or []):
            if isinstance(ph, dict):
                url = _photo_url((ph.get("mixedSources") or {}).get("jpeg"))
                if url:
                    photos.append({"url": url,
                                   "caption": ph.get("caption") or ""})
        reso = prop.get("resoFacts") if isinstance(prop.get("resoFacts"), dict) else {}
        return {
            "description": desc if isinstance(desc, str) else None,
            "photo_count": prop.get("photoCount"),
            "photos": photos[:MAX_PHOTOS_PER_LISTING * 2],
            "home_insights": prop.get("homeInsights"),
            "living_area": prop.get("livingAreaValue"),
            "bedrooms": prop.get("bedrooms"),
            "bathrooms": prop.get("bathrooms"),
            "home_type": prop.get("homeType"),
            "year_built": reso.get("yearBuilt"),
            "date_posted": prop.get("datePosted"),
            "street_address": prop.get("streetAddress"),
        }
    return None


# ---------------------------------------------------------------------------
# Checkpoint / lock helpers
# ---------------------------------------------------------------------------

def load_done() -> dict:
    """Map of already-harvested row id -> last JSONL record."""
    done = {}
    if os.path.exists(HARVEST_JSONL):
        with open(HARVEST_JSONL, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    done[rec["id"]] = rec
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def append_record(rec: dict) -> None:
    with open(HARVEST_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def zillow_sweep_running(conn: sqlite3.Connection) -> bool:
    """True while the cron pass holds an open 'zillow' scrape_run."""
    row = conn.execute(
        "SELECT COUNT(*) FROM scrape_runs "
        "WHERE source='zillow' AND status='running'").fetchone()
    return bool(row and row[0])


def wait_for_sweep(conn: sqlite3.Connection) -> None:
    while zillow_sweep_running(conn):
        logger.info("zillow cron sweep is running — pausing harvest 120s")
        time.sleep(120)


# ---------------------------------------------------------------------------
# Fetch + photo download
# ---------------------------------------------------------------------------

def fetch_and_classify(url: str, base_name: str,
                       allow_photos: bool) -> tuple[Optional[dict], list, Optional[str]]:
    """One fresh-profile page load -> (harvest record, saved photo paths, verdict).

    Parse and classify happen inside the SAME profile that might download the
    first photos — Zillow walls a session's second navigation, so the photo
    download must ride the page that already loaded the listing. Ambiguous
    listings (or 'vacant' text with a big gallery) only fetch photos when
    ``allow_photos`` is set (global image-budget check lives in the caller).
    """
    for attempt in (1, 2):
        try:
            with camoufox_context() as page:
                page.set_default_timeout(PAGE_TIMEOUT_MS)
                resp = page.goto(url, wait_until="domcontentloaded",
                                 timeout=PAGE_TIMEOUT_MS)
                html = resp.text() if resp else ""
                if len(html or "") < 100_000:
                    logger.warning("detail page too small (%d) for %s%s",
                                   len(html or ""), url,
                                   " — retrying" if attempt == 1 else "")
                    continue
                rec = extract_rich(html)
                if rec is None:
                    logger.warning("no SSR payload for %s%s", url,
                                   " — retrying" if attempt == 1 else "")
                    continue
                verdict = classify_listing(rec.get("description"),
                                           rec.get("home_insights"), rec)
                saved: list[str] = []
                if allow_photos and needs_images(verdict, rec.get("photo_count")):
                    for i, ph in enumerate((rec.get("photos") or [])
                                           [:MAX_PHOTOS_PER_LISTING]):
                        time.sleep(0.3)
                        try:
                            img = page.request.get(ph["url"],
                                                   timeout=PAGE_TIMEOUT_MS)
                            body = img.body() if img.ok else None
                            if body:
                                path = os.path.join(PHOTOS_DIR,
                                                    f"{base_name}_{i}.jpg")
                                with open(path, "wb") as out:
                                    out.write(body)
                                saved.append(path)
                        except Exception as exc:
                            logger.warning("photo fetch failed %s: %s",
                                           ph["url"], exc)
                return rec, saved, verdict
        except Exception as exc:
            logger.warning("detail fetch failed for %s (attempt %d): %s",
                           url, attempt, exc)
            time.sleep(5)
    return None, [], None


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def target_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, url, source_listing_id, address, city, county, "
        "       price_cents, acres "
        "FROM properties "
        "WHERE status='active' AND state='NC' AND source='zillow' "
        "  AND bedrooms IS NULL AND bathrooms IS NULL "
        "  AND url IS NOT NULL "
        "ORDER BY county, id").fetchall()


def store_description(conn: sqlite3.Connection, row_id: int,
                      description: Optional[str]) -> None:
    """Backfill listing text onto the row (never clobbers existing text)."""
    if not description:
        return
    conn.execute(
        "UPDATE properties SET description = ? "
        "WHERE id = ? AND (description IS NULL OR TRIM(description) = '')",
        (description.strip(), row_id))
    conn.commit()


def run_pass(limit: Optional[int] = None, write_db: bool = True,
             download_photos: bool = True) -> dict:
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    conn = _ensure_db()
    done = load_done()
    rows = [r for r in target_rows(conn) if r["id"] not in done]
    if limit:
        rows = rows[:limit]
    stats = {"target": len(rows), "harvested": 0, "failed": 0,
             "has_building": 0, "no_building": 0, "ambiguous": 0,
             "photo_sets": 0}
    logger.info("NC building harvest: %d row(s) to fetch (%d already done)",
                len(rows), len(done))
    image_sets = sum(1 for r in done.values() if r.get("photos_saved"))
    for i, row in enumerate(rows, 1):
        wait_for_sweep(conn)
        rec, saved, verdict = fetch_and_classify(
            row["url"], str(row["id"]),
            download_photos and image_sets < MAX_IMAGE_SETS)
        if rec is not None:
            if saved:
                image_sets += 1
                stats["photo_sets"] += 1
            out = {"id": row["id"], "zpid": row["source_listing_id"],
                   "url": row["url"], "county": row["county"],
                   "address": row["address"], "city": row["city"],
                   "status": "ok", "verdict": verdict,
                   "photos_saved": saved,
                   **{k: rec.get(k) for k in
                      ("description", "photo_count", "home_type",
                       "living_area", "year_built", "bedrooms", "bathrooms",
                       "date_posted")}}
            if verdict == "has_building" and rec.get("description"):
                out["evidence"] = existing_evidence(rec["description"])
            # Drop the raw insights blob from the log line but keep the phrases.
            phrases = []
            for group in rec.get("home_insights") or []:
                if isinstance(group, dict):
                    for item in group.get("insights", []) or []:
                        if isinstance(item, dict):
                            phrases.extend(item.get("phrases") or [])
            out["insight_phrases"] = sorted(set(phrases))[:8]
            append_record(out)
            if write_db:
                store_description(conn, row["id"], rec.get("description"))
            stats["harvested"] += 1
            stats[verdict] += 1
        else:
            append_record({"id": row["id"], "zpid": row["source_listing_id"],
                           "url": row["url"], "county": row["county"],
                           "address": row["address"], "status": "failed"})
            stats["failed"] += 1
        if i % 20 == 0 or i == len(rows):
            logger.info("NC building harvest %d/%d — has=%d none=%d amb=%d "
                        "fail=%d photo_sets=%d", i, len(rows),
                        stats["has_building"], stats["no_building"],
                        stats["ambiguous"], stats["failed"], stats["photo_sets"])
        time.sleep(random.uniform(*DELAY_BETWEEN_DETAILS))
    logger.info("NC building harvest DONE: %s", stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Harvest Zillow details for active NC no-bed/bath rows")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap rows this invocation (smoke tests)")
    parser.add_argument("--no-db", action="store_true",
                        help="skip the description backfill into SQLite")
    parser.add_argument("--no-photos", action="store_true",
                        help="skip downloading photos for ambiguous listings")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_pass(limit=args.limit, write_db=not args.no_db,
             download_photos=not args.no_photos)


if __name__ == "__main__":
    main()
