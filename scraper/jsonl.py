"""Resumable JSONL checkpoints for the long one-off passes.

The HOA sweep and the building harvest each walk thousands of detail pages
over several hours, so each keeps an append-only verdict log it can resume
from. Both had written the same two helpers, and the sweep additionally
imported the "pause while cron is working" helper *backwards* out of the
harvest module. The three now live here, next to each other.

A checkpoint line is one JSON object per row, keyed by ``id``. The last
record for an id wins (a re-judge appends over the earlier verdict) and an
unreadable line is skipped — a half-written line at the end of a killed run
must not abort a pass that already has hours of work in it.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, Union

from .db import fail_stale_runs

logger = logging.getLogger(__name__)

__all__ = ["load_done", "append_record", "write_records",
           "sweep_running", "wait_for_source"]

PathLike = Union[str, "os.PathLike[str]"]


def load_done(path: PathLike, key: str = "id") -> Dict[Any, dict]:
    """Map of already-processed row key -> last record written for it.

    Missing file yields ``{}`` so a first run and a resumed one read the
    same way. Values are the raw records; callers read whatever fields they
    wrote (``status``, ``verdict``, ``photos_saved``, …).
    """
    done: Dict[Any, dict] = {}
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
                done[int(rec[key])] = rec
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return done


def append_record(path: PathLike, rec: dict) -> None:
    """Append one checkpoint record (creates the file on first write)."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_records(path: PathLike, records: Iterable[dict]) -> None:
    """Append a batch of records with a single open file."""
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def sweep_running(conn: sqlite3.Connection, source: str = "zillow") -> bool:
    """True while a scheduled pass holds an open ``scrape_runs`` row."""
    row = conn.execute(
        "SELECT COUNT(*) FROM scrape_runs "
        "WHERE source=? AND status='running'", (source,)).fetchone()
    return bool(row and row[0])


def wait_for_source(conn: sqlite3.Connection, source: str = "zillow",
                    poll_seconds: int = 120) -> None:
    """Block while a cron pass on ``source`` is running, then return.

    Two one-off passes on the same site fight for the same anti-bot budget,
    so the offline sweeps defer to the scheduled one. A killed run leaves
    its row ``running`` forever, so stale rows are cleared first — live
    2026-11-08, a dead Zillow run wedged the HOA rejudge for two days.
    """
    fail_stale_runs(conn)
    while sweep_running(conn, source):
        logger.info("%s cron sweep is running — pausing %ds", source,
                    poll_seconds)
        time.sleep(poll_seconds)
