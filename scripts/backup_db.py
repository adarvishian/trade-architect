#!/usr/bin/env python3
"""Create a dated SQLite backup of the Trading Architect database.

Uses sqlite3.Connection.backup() so backups are consistent even when WAL mode
is enabled. Output lands in data/backups/ by default (git-ignored).

Usage:
    python scripts/backup_db.py
    python scripts/backup_db.py --db data/trading_architect.db --dest data/backups
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("data/trading_architect.db")
DEFAULT_DEST = Path("data/backups")


def backup_db(db_path: Path, dest_dir: Path) -> Path:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup Trading Architect SQLite database")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Source database path")
    parser.add_argument(
        "--dest", type=Path, default=DEFAULT_DEST, help="Destination directory for backups"
    )
    args = parser.parse_args()

    out = backup_db(args.db, args.dest)
    print(f"Backup written: {out}")


if __name__ == "__main__":
    main()
