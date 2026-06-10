"""Integration tests against real broker sample CSVs."""

from pathlib import Path

import pytest

from trading_architect.assembly.positions import assemble_positions
from trading_architect.bootstrap import import_and_assemble
from trading_architect.ingestion.robinhood import RobinhoodAdapter
from trading_architect.ingestion.schwab import SchwabAdapter
from trading_architect.ingestion.tradovate import TradovateAdapter

SAMPLES = Path(__file__).resolve().parents[1] / "sample csv"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)
    yield db_path


def test_robinhood_sample_parses_hundreds_of_events():
    adapter = RobinhoodAdapter()
    events, review = adapter.parse_csv(SAMPLES / "Robinhood Sample.csv")
    assert len(events) > 400
    assert len(review) < 20
    options = [e for e in events if e.asset_type.value == "option"]
    stocks = [e for e in events if e.asset_type.value == "stock"]
    assert len(options) > 50
    assert len(stocks) > 50
    assert all(e.price >= 0 for e in events)


def test_schwab_sample_parses_options_and_stocks():
    adapter = SchwabAdapter()
    events, review = adapter.parse_csv(
        SAMPLES / "Charles Schwab Transactions May 26 2026 Sample.csv"
    )
    assert len(events) > 150
    assert len(review) < 30
    symbols = {e.underlying for e in events}
    assert "MARA" in symbols or "TSLA" in symbols or "PLTR" in symbols


def test_tradovate_performance_sample_emits_entry_exit_pairs():
    adapter = TradovateAdapter()
    events, review = adapter.parse_csv(SAMPLES / "Tradovate Performance Sample.csv")
    assert len(events) == 38  # 19 round trips x 2 fills
    assert len(review) == 0
    assert all(e.asset_type.value == "future" for e in events)
    assert all(e.fill_id for e in events)


def test_tradovate_performance_pnl_matches_csv():
    from trading_architect.assembly.positions import assemble_positions

    adapter = TradovateAdapter()
    events, _ = adapter.parse_csv(SAMPLES / "Tradovate Performance Sample.csv")
    positions = assemble_positions(events)
    assert len(positions) == 19
    assert sum(p.realized_pnl for p in positions) == pytest.approx(263.75)
    wins = [p for p in positions if p.realized_pnl > 0]
    losses = [p for p in positions if p.realized_pnl < 0]
    assert len(wins) == 6
    assert len(losses) == 13
    assert sum(p.realized_pnl for p in wins) == pytest.approx(792.50)
    assert sum(p.realized_pnl for p in losses) == pytest.approx(-528.75)


def test_tradovate_performance_import_is_idempotent(temp_db):
    sample = SAMPLES / "Tradovate Performance Sample.csv"
    r1 = import_and_assemble(sample, "tradovate")
    r2 = import_and_assemble(sample, "tradovate")
    assert r1.imported == 38
    assert r2.imported == 0
    assert r2.skipped_duplicates == 38

    from trading_architect.bootstrap import create_repository

    repo = create_repository()
    positions = repo.list_positions()
    fut = [p for p in positions if p.silo.value == "futures"]
    assert len(fut) == 19
    assert sum(p.realized_pnl for p in fut) == pytest.approx(263.75)


def test_tradovate_reimport_dedupes_legacy_rows_without_fill_id(temp_db):
    """Legacy rows (pre fill-id parser) should not double P/L on reimport."""

    from trading_architect.bootstrap import create_repository
    from trading_architect.models.entities import TradeEvent

    sample = SAMPLES / "Tradovate Performance Sample.csv"
    repo = create_repository()
    adapter = TradovateAdapter()
    parsed, _ = adapter.parse_csv(sample)

    legacy = [
        TradeEvent(
            broker=e.broker,
            account=e.account,
            timestamp=e.timestamp.replace(tzinfo=None),
            symbol=e.symbol,
            underlying=e.underlying,
            asset_type=e.asset_type,
            side=e.side,
            quantity=e.quantity,
            price=e.price,
            silo=e.silo,
            raw_ref=f"legacy::{idx}",
        )
        for idx, e in enumerate(parsed[:2])
    ]
    repo.upsert_events(legacy)
    import_and_assemble(sample, "tradovate", repo=repo)

    events = repo.list_events()
    assert len(events) == 38
    positions = repo.list_positions()
    fut = [p for p in positions if p.silo.value == "futures"]
    assert sum(p.realized_pnl for p in fut) == pytest.approx(263.75)


def test_full_import_pipeline_on_samples(temp_db):
    r1 = import_and_assemble(
        SAMPLES / "Robinhood Sample.csv",
        "robinhood",
        account="robinhood-individual",
    )
    r2 = import_and_assemble(
        SAMPLES / "Charles Schwab Transactions May 26 2026 Sample.csv",
        "schwab",
        account="schwab-tos",
    )
    r3 = import_and_assemble(
        SAMPLES / "Tradovate Performance Sample.csv",
        "tradovate",
    )
    assert r1.imported > 0
    assert r2.imported > 0
    assert r3.imported > 0

    from trading_architect.bootstrap import create_repository

    repo = create_repository()
    positions = assemble_positions(repo.list_events())
    assert len(positions) > 10
