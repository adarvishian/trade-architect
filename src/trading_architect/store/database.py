"""SQLite normalized data store."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from trading_architect.config.defaults import DB_PATH, DEFAULT_EPOCHS
from trading_architect.models.entities import (
    ChainSnapshot,
    MethodologyEpoch,
    Position,
    ReviewQueueItem,
    TradeEvent,
)
from trading_architect.store.backup import ensure_daily_backup

SCHEMA = """
CREATE TABLE IF NOT EXISTS methodology_epochs (
    epoch_id TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date TEXT,
    label TEXT NOT NULL,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS trade_events (
    event_id TEXT PRIMARY KEY,
    natural_key TEXT UNIQUE NOT NULL,
    content_fingerprint TEXT,
    broker TEXT NOT NULL,
    account TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    underlying TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fees REAL DEFAULT 0,
    option_spec_json TEXT,
    stop_price REAL,
    silo TEXT NOT NULL,
    raw_ref TEXT NOT NULL,
    epoch_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    silo TEXT NOT NULL,
    underlying TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    component_event_ids_json TEXT NOT NULL,
    blended_cost_basis_json TEXT,
    current_delta_notional REAL DEFAULT 0,
    premium_at_risk REAL DEFAULT 0,
    stop_risk REAL DEFAULT 0,
    total_dollar_risk REAL DEFAULT 0,
    epoch_id TEXT,
    realized_r REAL,
    open_r REAL,
    realized_pnl REAL DEFAULT 0,
    opened_at TEXT,
    closed_at TEXT,
    payload_json TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_queue (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    reason TEXT NOT NULL,
    raw_row_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    imported INTEGER,
    skipped_duplicates INTEGER,
    review_queue_count INTEGER,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chain_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    underlying TEXT NOT NULL,
    asof_timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_events_underlying ON trade_events(underlying);
CREATE INDEX IF NOT EXISTS idx_chain_snapshots_underlying ON chain_snapshots(underlying);

CREATE TABLE IF NOT EXISTS user_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS override_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parameter TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'settings_ui',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_events_silo ON trade_events(silo);
CREATE INDEX IF NOT EXISTS idx_trade_events_timestamp ON trade_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_positions_silo ON positions(silo);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    label TEXT NOT NULL UNIQUE,
    silo TEXT NOT NULL,
    institution TEXT DEFAULT '',
    include_in_deployable INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    as_of TEXT NOT NULL,
    cash REAL NOT NULL DEFAULT 0,
    equity_value REAL NOT NULL DEFAULT 0,
    buying_power REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS holdings_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    occ_symbol TEXT,
    asset_type TEXT NOT NULL,
    qty REAL NOT NULL DEFAULT 0,
    mark REAL,
    mtm_value REAL NOT NULL DEFAULT 0,
    cost_basis REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_balance_snapshots_account ON balance_snapshots(account_id);
CREATE INDEX IF NOT EXISTS idx_balance_snapshots_as_of ON balance_snapshots(as_of);
CREATE INDEX IF NOT EXISTS idx_holdings_snapshots_account ON holdings_snapshots(account_id);
CREATE INDEX IF NOT EXISTS idx_holdings_snapshots_as_of ON holdings_snapshots(as_of);

CREATE TABLE IF NOT EXISTS position_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    silo TEXT NOT NULL,
    initial_stop REAL,
    current_stop REAL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, silo)
);

CREATE TABLE IF NOT EXISTS size_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    silo TEXT NOT NULL,
    underlying TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    recommended_qty REAL NOT NULL,
    dollar_risk REAL NOT NULL,
    binding_constraint TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    silo_equity REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_size_recommendations_ts ON size_recommendations(ts);
CREATE INDEX IF NOT EXISTS idx_position_overrides_silo ON position_overrides(silo);
"""


class Database:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        ensure_daily_backup(self.path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_trade_events(conn)
            self._migrate_accounts(conn)
            self._migrate_phase2(conn)
            for epoch in DEFAULT_EPOCHS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO methodology_epochs
                    (epoch_id, start_date, end_date, label, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        epoch.epoch_id,
                        epoch.start_date.isoformat(),
                        epoch.end_date.isoformat() if epoch.end_date else None,
                        epoch.label,
                        epoch.notes,
                    ),
                )

    def _migrate_accounts(self, conn: sqlite3.Connection) -> None:
        """Additive migrations for account-first tables (Phase 1)."""
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "accounts" not in tables:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL UNIQUE,
                    silo TEXT NOT NULL,
                    institution TEXT DEFAULT '',
                    include_in_deployable INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS balance_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    as_of TEXT NOT NULL,
                    cash REAL NOT NULL DEFAULT 0,
                    equity_value REAL NOT NULL DEFAULT 0,
                    buying_power REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS holdings_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    as_of TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    occ_symbol TEXT,
                    asset_type TEXT NOT NULL,
                    qty REAL NOT NULL DEFAULT 0,
                    mark REAL,
                    mtm_value REAL NOT NULL DEFAULT 0,
                    cost_basis REAL NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_balance_snapshots_account
                    ON balance_snapshots(account_id);
                CREATE INDEX IF NOT EXISTS idx_balance_snapshots_as_of
                    ON balance_snapshots(as_of);
                CREATE INDEX IF NOT EXISTS idx_holdings_snapshots_account
                    ON holdings_snapshots(account_id);
                CREATE INDEX IF NOT EXISTS idx_holdings_snapshots_as_of
                    ON holdings_snapshots(as_of);
                """
            )

    def _migrate_phase2(self, conn: sqlite3.Connection) -> None:
        """Additive migrations for Phase 2 — position stops and size recommendations."""
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "position_overrides" not in tables:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS position_overrides (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    silo TEXT NOT NULL,
                    initial_stop REAL,
                    current_stop REAL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, silo)
                );
                CREATE INDEX IF NOT EXISTS idx_position_overrides_silo
                    ON position_overrides(silo);
                """
            )
        if "size_recommendations" not in tables:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS size_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    silo TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    recommended_qty REAL NOT NULL,
                    dollar_risk REAL NOT NULL,
                    binding_constraint TEXT NOT NULL,
                    inputs_json TEXT NOT NULL,
                    silo_equity REAL NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_size_recommendations_ts
                    ON size_recommendations(ts);
                """
            )

    def _migrate_trade_events(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_events)")}
        if "content_fingerprint" not in cols:
            conn.execute("ALTER TABLE trade_events ADD COLUMN content_fingerprint TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_events_content_fingerprint "
            "ON trade_events(content_fingerprint)"
        )
        rows = conn.execute(
            "SELECT event_id, payload_json FROM trade_events WHERE content_fingerprint IS NULL"
        ).fetchall()
        for row in rows:
            event = TradeEvent.model_validate(json.loads(row["payload_json"]))
            conn.execute(
                "UPDATE trade_events SET content_fingerprint = ? WHERE event_id = ?",
                (event.content_fingerprint, row["event_id"]),
            )

    def clear_positions(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM positions")

    def reset_all(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                DELETE FROM trade_events;
                DELETE FROM positions;
                DELETE FROM review_queue;
                DELETE FROM import_log;
                """
            )


def event_to_row(event: TradeEvent) -> tuple:
    payload = event.model_dump(mode="json")
    option_json = json.dumps(payload.get("option_spec")) if event.option_spec else None
    return (
        event.event_id,
        event.natural_key,
        event.content_fingerprint,
        event.broker,
        event.account,
        event.timestamp.isoformat(),
        event.symbol,
        event.underlying,
        event.asset_type.value,
        event.side.value,
        event.quantity,
        event.price,
        event.fees,
        option_json,
        event.stop_price,
        event.silo.value,
        event.raw_ref,
        event.epoch_id,
        json.dumps(payload),
    )


def row_to_event(row: sqlite3.Row) -> TradeEvent:
    return TradeEvent.model_validate(json.loads(row["payload_json"]))


def row_to_position(row: sqlite3.Row) -> Position:
    return Position.model_validate(json.loads(row["payload_json"]))


def row_to_epoch(row: sqlite3.Row) -> MethodologyEpoch:
    return MethodologyEpoch(
        epoch_id=row["epoch_id"],
        start_date=datetime.fromisoformat(row["start_date"]).date(),
        end_date=(datetime.fromisoformat(row["end_date"]).date() if row["end_date"] else None),
        label=row["label"],
        notes=row["notes"] or "",
    )


def row_to_chain_snapshot(row: sqlite3.Row) -> ChainSnapshot:
    return ChainSnapshot.model_validate(json.loads(row["payload_json"]))


def row_to_review_item(row: sqlite3.Row) -> ReviewQueueItem:
    return ReviewQueueItem(
        item_id=str(row["item_id"]),
        source_file=row["source_file"],
        row_index=row["row_index"],
        reason=row["reason"],
        raw_row=json.loads(row["raw_row_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
