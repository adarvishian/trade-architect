"""Tests for ingestion dedup and adapters."""

from pathlib import Path

import pytest

from trading_architect.bootstrap import create_repository, import_and_assemble
from trading_architect.ingestion.base import resolve_epoch_id
from trading_architect.ingestion.robinhood import RobinhoodAdapter


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)
    yield db_path


def test_robinhood_adapter_parses_stock_and_option():
    adapter = RobinhoodAdapter()
    events, review = adapter.parse_csv(FIXTURES / "robinhood_sample.csv")
    assert len(events) == 4
    assert len(review) == 0
    assert events[0].underlying == "AAPL"
    assert events[2].asset_type.value == "option"


def test_import_is_idempotent(temp_db):
    path = FIXTURES / "robinhood_sample.csv"
    r1 = import_and_assemble(path, "robinhood", account="test")
    r2 = import_and_assemble(path, "robinhood", account="test")
    assert r1.imported == 4
    assert r2.imported == 0
    assert r2.skipped_duplicates == 4


def test_epoch_resolution():
    from datetime import date

    assert resolve_epoch_id(date(2026, 1, 1)) == "pre-2026-03-27"
    assert resolve_epoch_id(date(2026, 4, 1)) == "post-2026-03-27"


def test_mixed_timezone_events_assemble_without_error():
    from datetime import datetime, timezone

    from trading_architect.assembly.positions import assemble_positions
    from trading_architect.models.entities import AssetType, Side, Silo, TradeEvent

    aware = TradeEvent(
        broker="schwab",
        account="schwab-tos",
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        symbol="AAPL",
        underlying="AAPL",
        asset_type=AssetType.STOCK,
        side=Side.BUY,
        quantity=1,
        price=100,
        silo=Silo.STOCK_OPTIONS,
        raw_ref="test::aware",
    )
    naive = TradeEvent(
        broker="tradovate",
        account="tradovate",
        timestamp=datetime(2026, 5, 11, 10, 45, 16),
        symbol="MESM6",
        underlying="MES",
        asset_type=AssetType.FUTURE,
        side=Side.BUY,
        quantity=1,
        price=7400,
        silo=Silo.FUTURES,
        raw_ref="test::naive",
    )
    positions = assemble_positions([aware, naive])
    assert len(positions) == 2


def test_schwab_and_tradovate_import(temp_db):
    r_schwab = import_and_assemble(FIXTURES / "schwab_sample.csv", "schwab")
    r_tv = import_and_assemble(FIXTURES / "tradovate_sample.csv", "tradovate")
    assert r_schwab.imported == 2
    assert r_tv.imported == 2

    repo = create_repository()
    positions = repo.list_positions()
    assert len(positions) >= 2
