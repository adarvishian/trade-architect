"""Milestone 1 — correctness fixes (WAL, mark fallbacks, options expiry filter)."""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from trading_architect.config.options_selector import OptionSelectorConfig
from trading_architect.engines.equity import compute_mark_fallback_stats
from trading_architect.engines.marks import Mark
from trading_architect.engines.options_selector import (
    OptionDirection,
    OptionSelectorInput,
    enumerate_candidates,
)
from trading_architect.models.entities import (
    ChainContract,
    ChainSnapshot,
    Direction,
    Position,
    PositionStatus,
    Silo,
)
from trading_architect.store.database import Database


def test_wal_concurrent_connections(tmp_path: Path):
    db_path = tmp_path / "concurrent.db"
    db = Database(db_path)
    db.initialize()
    errors: list[str] = []

    def writer():
        try:
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO import_log (source_file, imported, skipped_duplicates, review_queue_count) "
                    "VALUES ('test', 1, 0, 0)"
                )
        except Exception as exc:
            errors.append(str(exc))

    def reader():
        try:
            with db.connect() as conn:
                conn.execute("SELECT COUNT(*) FROM import_log").fetchone()
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_compute_mark_fallback_stats_counts_cost_basis_legs():
    pos = Position(
        position_id="p1",
        silo=Silo.STOCK_OPTIONS,
        underlying="XYZ",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"XYZ": 10.0},
        blended_cost_basis={"XYZ": 50.0},
    )
    stats = compute_mark_fallback_stats([pos], marks_by_symbol={})
    assert stats.legs_at_cost_basis == 1
    assert "XYZ" in stats.missing_mark_symbols


def test_compute_mark_fallback_stats_counts_iv_fallback():
    pos = Position(
        position_id="p1",
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"AAPL_2026-09-01_C_200": 1.0},
        blended_cost_basis={"AAPL_2026-09-01_C_200": 5.0},
    )
    live = {
        "AAPL_2026-09-01_C_200": Mark(
            symbol="AAPL_2026-09-01_C_200",
            price=6.0,
            delta=0.45,
            asof=datetime(2026, 5, 26),
            iv_fallback=True,
        )
    }
    stats = compute_mark_fallback_stats(
        [pos],
        marks_by_symbol={"AAPL_2026-09-01_C_200": 6.0},
        live_marks=live,
    )
    assert stats.legs_at_cost_basis == 0
    assert stats.delta_iv_fallbacks == 1


def test_enumerate_excludes_contracts_expiring_before_hold():
    snap = ChainSnapshot(
        underlying="X",
        asof_timestamp=datetime(2026, 5, 26),
        spot_price=100,
        contracts=[
            ChainContract(
                strike=100, expiry=datetime(2026, 7, 1).date(), dte=36, mid=2.0, right="C"
            ),
            ChainContract(
                strike=105, expiry=datetime(2026, 9, 1).date(), dte=98, mid=1.5, right="C"
            ),
        ],
    )
    inputs = OptionSelectorInput(
        underlying="X",
        direction=OptionDirection.LONG_CALL,
        target_price=110.0,
        expected_hold_days=60,
    )
    cfg = OptionSelectorConfig(min_dte=30, max_dte=365, dte_buffer_days=0)
    candidates = enumerate_candidates(snap, inputs, cfg)
    strikes = {c.strike for c in candidates}
    assert 100 not in strikes
    assert 105 in strikes
