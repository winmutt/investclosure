"""Daily SQLite backups with retention.

The 2026-10-06 incident (production DB deleted via a bad ``rm -f``; only a
6-week-old snapshot survived) showed a backup routine is mandatory. This
module:

- copies the live DB with the SQLite online-backup API (consistent even
  while scrapers hold write locks; plain ``cp`` can catch WAL mid-flight),
- names snapshots ``investclosure-YYYYMMDD-HHMMSS.sqlite3`` in
  ``config.backups_dir`` (``./data/backups``),
- prunes snapshots older than ``retention_days`` (default 7),
- is idempotent per day: :func:`backup_if_needed` skips when today's
  snapshot already exists, so the twice-daily cron backs up once.

Wiring: ``python3 -m scraper --backup`` (manual) and the top of every
:func:`cmd_cron` cycle in ``run.py``.
"""
from __future__ import annotations
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_SNAPSHOT_RE = re.compile(r"^investclosure-(\d{8})-\d{6}\.sqlite3$")


def _backup_dir() -> Path:
    from .config import config
    d = Path(config.backups_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_database(retention_days: int = 7) -> Dict[str, object]:
    """Snapshot the live DB; prune snapshots older than retention. Returns stats."""
    from .config import config
    src = Path(config.db_path)
    if not src.exists():
        return {"backed_up": None, "pruned": 0, "error": f"DB missing: {src}"}
    dest_dir = _backup_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"investclosure-{stamp}.sqlite3"
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        try:
            dst_conn = sqlite3.connect(str(dest))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except Exception as e:
        logger.error("DB backup failed: %s", e)
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return {"backed_up": None, "pruned": 0, "error": str(e)}
    pruned = prune_backups(retention_days)
    # Integrity check: the snapshot must open and hold the properties table.
    try:
        chk = sqlite3.connect(str(dest))
        try:
            n = chk.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
        finally:
            chk.close()
    except Exception as e:
        logger.error("Backup integrity check failed for %s: %s", dest.name, e)
        return {"backed_up": dest.name, "rows": None, "pruned": pruned,
                "error": f"integrity check failed: {e}"}
    logger.info("DB backup complete: %s (%d properties, pruned %d)",
                dest.name, n, pruned)
    return {"backed_up": dest.name, "rows": n, "pruned": pruned}


def prune_backups(retention_days: int = 7) -> int:
    """Delete snapshots older than retention_days. Returns count removed."""
    dest_dir = _backup_dir()
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0
    for f in dest_dir.iterdir():
        m = _SNAPSHOT_RE.match(f.name)
        if not m:
            continue  # never touch foreign files (e.g. *.bak-*)
        try:
            fdate = datetime.strptime(m.group(1), "%Y%m%d")
        except ValueError:
            continue
        if fdate < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
            try:
                f.unlink()
                removed += 1
            except Exception as e:
                logger.warning("Could not prune %s: %s", f.name, e)
    return removed


def backup_if_needed(retention_days: int = 7) -> Dict[str, object]:
    """Back up only when no snapshot from today exists (cron-safe)."""
    today = date.today().strftime("%Y%m%d")
    dest_dir = _backup_dir()
    for f in dest_dir.iterdir():
        m = _SNAPSHOT_RE.match(f.name)
        if m and m.group(1) == today:
            pruned = prune_backups(retention_days)
            return {"backed_up": None, "skipped": f.name, "pruned": pruned}
    return backup_database(retention_days)
