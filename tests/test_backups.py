"""Backup routine: snapshot + 7-day retention."""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from scraper import backups


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE properties (id INTEGER PRIMARY KEY, address TEXT)")
    conn.execute("INSERT INTO properties (address) VALUES ('1 Main St')")
    conn.commit()
    conn.close()


def test_backup_and_prune(tmp_path, monkeypatch):
    from scraper import config as config_mod
    db_path = tmp_path / "investclosure.db"
    bak_dir = tmp_path / "backups"
    _make_db(db_path)
    monkeypatch.setattr(config_mod.config, "db_path", db_path)
    monkeypatch.setattr(config_mod.config, "backups_dir", bak_dir)

    res = backups.backup_database()
    assert res["backed_up"] and res["rows"] == 1
    assert len(list(bak_dir.glob("*.sqlite3"))) == 1

    # Idempotent per day: second call skips.
    res2 = backups.backup_if_needed()
    assert res2["backed_up"] is None and res2["skipped"]

    # An 8-day-old snapshot gets pruned; today's survives.
    old = bak_dir / f"investclosure-{(datetime.now() - timedelta(days=8)).strftime('%Y%m%d')}-000000.sqlite3"
    old.write_bytes(b"junk")
    assert backups.prune_backups(7) == 1
    assert not old.exists()
    assert len(list(bak_dir.glob("*.sqlite3"))) == 1

    # Foreign files are never touched.
    keep = bak_dir / "investclosure.db.bak-legacy"
    keep.write_bytes(b"junk")
    backups.prune_backups(0)
    assert keep.exists()


def test_backup_missing_db(tmp_path, monkeypatch):
    from scraper import config as config_mod
    monkeypatch.setattr(config_mod.config, "db_path", tmp_path / "nope.db")
    monkeypatch.setattr(config_mod.config, "backups_dir", tmp_path / "backups")
    res = backups.backup_database()
    assert res["backed_up"] is None and res["error"]
