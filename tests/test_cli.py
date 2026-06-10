"""CLI smoke tests — each subcommand parses and runs against a tmp DB."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trading_architect.bootstrap import create_repository, import_and_assemble
from trading_architect.cli import main
from trading_architect.models.entities import AssetType, ImportResult, Side, Silo, TradeEvent

FIXTURES = Path(__file__).parent / "fixtures"
CHAIN = FIXTURES / "chain_sample.csv"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)
    yield db_path


@pytest.fixture
def seeded_db(temp_db):
    import_and_assemble(FIXTURES / "robinhood_sample.csv", "robinhood", account="test-cli")
    return temp_db


def _run_cli(argv: list[str]) -> str:
    with patch.object(sys, "argv", ["ta", *argv]):
        main()
    return ""


def test_cli_help(capsys):
    with patch.object(sys, "argv", ["ta"]):
        main()
    out = capsys.readouterr().out
    assert "usage:" in out.lower() or "positional arguments" in out.lower()


def test_status_empty_db(temp_db, capsys):
    with patch.object(sys, "argv", ["ta", "status"]):
        main()
    out = capsys.readouterr().out
    assert "Events: 0" in out
    assert "Positions: 0" in out


def test_import_robinhood_csv(temp_db, capsys):
    with patch.object(
        sys,
        "argv",
        [
            "ta",
            "import",
            str(FIXTURES / "robinhood_sample.csv"),
            "--broker",
            "robinhood",
            "--account",
            "test",
        ],
    ):
        main()
    out = capsys.readouterr().out
    assert "Imported 4 events" in out
    assert "Positions:" in out


def test_import_tradovate_csv(temp_db, capsys):
    with patch.object(
        sys,
        "argv",
        [
            "ta",
            "import",
            str(FIXTURES / "tradovate_sample.csv"),
            "--broker",
            "tradovate",
        ],
    ):
        main()
    out = capsys.readouterr().out
    assert "Imported 2 events" in out


def test_marks_no_positions(temp_db, capsys):
    with patch.object(sys, "argv", ["ta", "marks"]):
        main()
    out = capsys.readouterr().out
    assert "No open positions" in out


def test_size_stock(capsys):
    with patch.object(
        sys,
        "argv",
        [
            "ta",
            "size",
            "--silo",
            "stock_options",
            "--underlying",
            "AAPL",
            "--entry",
            "100",
            "--stop",
            "95",
            "--equity",
            "100000",
        ],
    ):
        main()
    out = capsys.readouterr().out
    assert "Binding constraint" in out or "recommended" in out.lower()


def test_evaluate_no_events(temp_db, capsys):
    with patch.object(sys, "argv", ["ta", "evaluate"]):
        main()
    out = capsys.readouterr().out
    assert "No events in store" in out


def test_evaluate_with_events(seeded_db, capsys):
    with patch.object(
        sys,
        "argv",
        ["ta", "evaluate", "--stock-equity", "100000", "--futures-equity", "50000"],
    ):
        main()
    out = capsys.readouterr().out
    assert "Alpha" in out or "alpha" in out or "evaluation" in out.lower()


def test_risk_no_events(temp_db, capsys):
    with patch.object(sys, "argv", ["ta", "risk"]):
        main()
    out = capsys.readouterr().out
    assert "No events in store" in out


def test_risk_with_events(seeded_db, capsys):
    with patch.object(sys, "argv", ["ta", "risk", "--silo", "stock_options"]):
        main()
    out = capsys.readouterr().out
    assert "Kelly" in out or "kelly" in out or "risk" in out.lower()


def test_options_csv_rank(capsys):
    with patch.object(
        sys,
        "argv",
        [
            "ta",
            "options",
            "--chain",
            str(CHAIN),
            "--underlying",
            "NVDA",
            "--spot",
            "200",
            "--target",
            "210",
            "--hold-days",
            "60",
            "--direction",
            "long_call",
            "--equity",
            "100000",
            "--top",
            "3",
        ],
    ):
        main()
    out = capsys.readouterr().out
    assert "Rank" in out or "rank" in out or "premium" in out.lower()


def test_fetch_schwab_login_missing_package(capsys):
    with patch(
        "trading_architect.ingestion.schwab_auth.schwab_py_available",
        return_value=False,
    ):
        with patch.object(sys, "argv", ["ta", "fetch-schwab", "--login"]):
            main()
    out = capsys.readouterr().out
    assert "schwab-py" in out.lower()


@patch("trading_architect.bootstrap.import_robinhood_fetch")
def test_fetch_robinhood_mocked(mock_fetch, temp_db, capsys):
    mock_fetch.return_value = ImportResult(imported=3, skipped_duplicates=0, source_file="rh")
    with patch.object(sys, "argv", ["ta", "fetch-robinhood", "--account", "robinhood-roth"]):
        main()
    out = capsys.readouterr().out
    assert "Fetched and imported 3 events" in out
    assert "Positions:" in out


@patch("trading_architect.ingestion.robinhood_fetch.fetch_portfolio_snapshot")
def test_fetch_robinhood_portfolio_mocked(mock_snapshot, capsys):
    from trading_architect.models.entities import BrokerAccountBalance, RobinhoodPortfolioSnapshot

    mock_snapshot.return_value = RobinhoodPortfolioSnapshot(
        fetched_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        accounts=[
            BrokerAccountBalance(
                account="robinhood-roth",
                cash=1000.0,
                cash_equivalents=500.0,
                cash_and_equivalents=1500.0,
                portfolio_equity=25000.0,
                buying_power=5000.0,
            )
        ],
        holdings=[],
    )
    with patch.object(sys, "argv", ["ta", "fetch-robinhood-portfolio"]):
        main()
    out = capsys.readouterr().out
    assert "Robinhood portfolio" in out
    assert "Total cash" in out


@patch("trading_architect.bootstrap.fetch_schwab_portfolio_snapshot")
@patch("trading_architect.ingestion.schwab_auth.get_client")
def test_accounts_snapshot_mocked(mock_client, mock_snapshot, temp_db, capsys):
    from trading_architect.models.entities import BrokerAccountBalance, SchwabAccountSnapshot

    mock_client.return_value = MagicMock()
    mock_snapshot.return_value = SchwabAccountSnapshot(
        fetched_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        accounts=[
            BrokerAccountBalance(
                account="schwab-tos",
                cash=5000.0,
                cash_equivalents=0.0,
                cash_and_equivalents=5000.0,
                portfolio_equity=120000.0,
                buying_power=80000.0,
            )
        ],
        holdings=[],
    )
    with patch.object(sys, "argv", ["ta", "accounts", "snapshot"]):
        main()
    out = capsys.readouterr().out
    assert "Schwab portfolio" in out
    assert "liquidationValue" in out or "MTM equity" in out


@patch("trading_architect.bootstrap.import_schwab_fetch")
def test_fetch_schwab_mocked(mock_fetch, temp_db, capsys):
    mock_fetch.return_value = ImportResult(
        imported=5,
        skipped_duplicates=1,
        source_file="schwab-api",
        review_queue=0,
    )
    with patch.object(
        sys,
        "argv",
        ["ta", "fetch-schwab", "--account", "schwab-tos", "--start", "2026-01-01"],
    ):
        main()
    out = capsys.readouterr().out
    assert "Fetched and imported 5 events" in out


def test_marks_with_open_position(temp_db, capsys):
    repo = create_repository()
    event = TradeEvent(
        broker="test",
        account="test",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        symbol="AAPL",
        underlying="AAPL",
        asset_type=AssetType.STOCK,
        side=Side.BUY,
        quantity=10,
        price=150.0,
        silo=Silo.STOCK_OPTIONS,
        raw_ref="cli::marks",
    )
    repo.upsert_events([event])
    from trading_architect.assembly.positions import assemble_positions

    repo.replace_positions(assemble_positions(repo.list_events()))

    with patch(
        "trading_architect.engines.marks.default_marks_provider",
    ) as mock_provider:
        mock_provider.return_value.marks_for.return_value = {}
        with patch.object(sys, "argv", ["ta", "marks"]):
            main()
    out = capsys.readouterr().out
    assert "No marks available" in out or "Mark fallbacks" in out
