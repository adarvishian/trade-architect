"""SQLite backup with daily rotation (Phase 1 ops hardening)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DEST = Path("data/backups")
_KEEP_DAYS = 14


def backup_db(db_path: Path, dest_dir: Path) -> Path:
    """Consistent backup via sqlite3.Connection.backup() with WAL checkpoint."""
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = dest_dir / f"trading_architect_{stamp}.db"

    src = sqlite3.connect(db_path)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst = sqlite3.connect(out_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    return out_path


def _prune_old_backups(dest_dir: Path, *, keep_days: int = _KEEP_DAYS) -> None:
    if not dest_dir.is_dir():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    for path in sorted(dest_dir.glob("trading_architect_*.db")):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            path.unlink(missing_ok=True)


def ensure_daily_backup(db_path: Path, dest_dir: Path | None = None) -> Path | None:
    """Create at most one backup per UTC day; prune backups older than 14 days."""
    if not db_path.is_file():
        return None

    dest = dest_dir or db_path.parent / "backups"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    for existing in dest.glob(f"trading_architect_{today}*.db"):
        if existing.is_file():
            _prune_old_backups(dest)
            return existing

    out = backup_db(db_path, dest)
    _prune_old_backups(dest)
    return out
