"""Database backup rotation."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from trading_architect.store.backup import backup_db, ensure_daily_backup


def test_backup_db_creates_file(tmp_path):
    db_path = tmp_path / "test.db"
    sqlite3.connect(db_path).close()
    out = backup_db(db_path, tmp_path / "backups")
    assert out.is_file()


def test_ensure_daily_backup_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()

    dest = tmp_path / "backups"
    first = ensure_daily_backup(db_path, dest)
    second = ensure_daily_backup(db_path, dest)
    assert first is not None
    assert second == first
    assert len(list(dest.glob("trading_architect_*.db"))) == 1


def test_prune_old_backups(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    sqlite3.connect(db_path).close()
    dest = tmp_path / "backups"
    dest.mkdir()
    old = dest / "trading_architect_20200101T000000Z.db"
    old.write_bytes(b"")
    old_time = datetime.now(timezone.utc) - timedelta(days=30)
    import os

    os.utime(old, (old_time.timestamp(), old_time.timestamp()))
    ensure_daily_backup(db_path, dest)
    assert not old.exists()
