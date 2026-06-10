"""Direct repository-layer persistence tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trading_architect.config.user_settings import AppSettings
from trading_architect.models.entities import (
    AssetType,
    Direction,
    MethodologyEpoch,
    Position,
    PositionStatus,
    ReviewQueueItem,
    Side,
    Silo,
    TradeEvent,
)
from trading_architect.store.database import Database
from trading_architect.store.repository import Repository

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


@pytest.fixture
def repo(tmp_path) -> Repository:
    db = Database(tmp_path / "repo_test.db")
    return Repository(db)


def _event(**overrides) -> TradeEvent:
    defaults = dict(
        broker="tradovate",
        account="tradovate",
        timestamp=datetime(2026, 5, 11, 10, 45, 16, tzinfo=timezone.utc),
        symbol="MCLM6",
        underlying="MCL",
        asset_type=AssetType.FUTURE,
        side=Side.BUY,
        quantity=1,
        price=98.23,
        silo=Silo.FUTURES,
        raw_ref="repo::row_0",
    )
    defaults.update(overrides)
    return TradeEvent(**defaults)


def test_migration_idempotent(tmp_path):
    db_path = tmp_path / "migrate.db"
    db = Database(db_path)
    db.initialize()
    epoch_count = 0
    with db.connect() as conn:
        epoch_count = conn.execute("SELECT COUNT(*) FROM methodology_epochs").fetchone()[0]
    db.initialize()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM methodology_epochs").fetchone()[0] == epoch_count


def test_settings_roundtrip(repo: Repository):
    settings = AppSettings()
    settings.starting_equity_stock_options = 125_000.0
    settings.sizing.base_risk_f = 0.012
    repo.save_app_settings(settings)

    loaded = repo.load_app_settings()
    assert loaded.starting_equity_stock_options == 125_000.0
    assert loaded.sizing.base_risk_f == 0.012


def test_review_queue_persisted(repo: Repository):
    items = [
        ReviewQueueItem(
            source_file="test.csv",
            row_index=3,
            reason="bad row",
            raw_row={"col": "value"},
        )
    ]
    repo.add_review_items(items)
    queued = repo.list_review_queue()
    assert len(queued) == 1
    assert queued[0].reason == "bad row"
    assert queued[0].row_index == 3


def test_replace_positions_atomic(repo: Repository):
    pos = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        component_event_ids=["e1"],
        blended_cost_basis={"AAPL": 100.0},
        leg_net_qty={"AAPL": 10.0},
    )
    repo.replace_positions([pos])
    assert repo.position_count() == 1

    repo.replace_positions([])
    assert repo.position_count() == 0


def test_connect_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "rollback.db")
    db.initialize()

    with pytest.raises(RuntimeError, match="simulated failure"):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO import_log (source_file, imported, skipped_duplicates, review_queue_count)
                VALUES ('test', 1, 0, 0)
                """
            )
            raise RuntimeError("simulated failure")

    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM import_log").fetchone()["c"]
    assert count == 0


def test_upsert_batch_rolls_back_on_failure(repo: Repository):
    events = [
        _event(raw_ref="batch::0"),
        _event(raw_ref="batch::1", symbol="MESM6", underlying="MES"),
    ]
    insert_calls = {"n": 0}
    real_connect = repo.db.connect

    class PatchedConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if "INSERT OR IGNORE INTO trade_events" in sql:
                insert_calls["n"] += 1
                if insert_calls["n"] == 2:
                    raise RuntimeError("simulated insert failure")
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    from contextlib import contextmanager

    @contextmanager
    def patched_connect():
        with real_connect() as conn:
            yield PatchedConnection(conn)

    with patch.object(repo.db, "connect", patched_connect):
        with pytest.raises(RuntimeError, match="simulated insert failure"):
            repo.upsert_events(events)

    assert repo.event_count() == 0
    assert repo.list_import_log() == []


def test_cross_fingerprint_natural_key_upgrade(repo: Repository):
    legacy = _event(raw_ref="legacy::0")
    repo.upsert_events([legacy])
    assert repo.event_count() == 1
    assert repo.list_events()[0].fill_id is None

    keyed = _event(fill_id="FILL-123", raw_ref="keyed::0")
    result = repo.upsert_events([keyed])
    assert result.imported == 0
    assert result.skipped_duplicates == 0
    stored = repo.list_events()[0]
    assert stored.fill_id == "FILL-123"
    assert repo.event_count() == 1


def test_update_epoch(repo: Repository):
    epochs = repo.list_epochs()
    assert len(epochs) >= 1
    epoch = epochs[0]
    updated = MethodologyEpoch(
        epoch_id=epoch.epoch_id,
        start_date=epoch.start_date,
        end_date=epoch.end_date,
        label="Updated label",
        notes=epoch.notes,
    )
    repo.update_epoch(updated)
    reloaded = next(e for e in repo.list_epochs() if e.epoch_id == epoch.epoch_id)
    assert reloaded.label == "Updated label"


def test_import_log_written(repo: Repository):
    repo.upsert_events([_event()])
    log = repo.list_import_log(limit=5)
    assert len(log) == 1
    assert log[0]["imported"] == 1
