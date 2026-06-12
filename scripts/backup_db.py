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
from pathlib import Path

from trading_architect.store.backup import backup_db

DEFAULT_DB = Path("data/trading_architect.db")
DEFAULT_DEST = Path("data/backups")


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
