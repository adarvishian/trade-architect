"""Phase 1 — holdings-first current state and snapshot equity."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_architect.bootstrap import build_app_book_context
from trading_architect.config.user_settings import AppSettings
from trading_architect.models.entities import Silo
from trading_architect.services.current_state import (
    current_positions,
    silo_equity_from_snapshots,
    silo_peak_equity_from_snapshots,
)
from trading_architect.store.database import Database
from trading_architect.store.repository import (
    BalanceSnapshotRecord,
    HoldingSnapshotRecord,
    Repository,
)


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "current_state.db"))


def _seed_schwab(repo: Repository) -> None:
    acct = repo.upsert_account(kind="schwab", label="schwab-tos", silo=Silo.STOCK_OPTIONS)
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=as_of,
            cash=12_000.0,
            equity_value=125_000.5,
            buying_power=85_000.0,
            source="api",
        )
    )
    repo.record_holdings_snapshots(
        acct.id,
        as_of,
        [
            HoldingSnapshotRecord(
                account_id=acct.id,
                as_of=as_of,
                symbol="AAPL",
                asset_type="stock",
                qty=10.0,
                mark=150.0,
                mtm_value=1_500.0,
                cost_basis=140.0,
            ),
        ],
    )


def test_silo_equity_from_snapshots(repo: Repository):
    settings = AppSettings(starting_equity_stock_options=100_000.0)
    equity, as_of = silo_equity_from_snapshots(repo, Silo.STOCK_OPTIONS, settings)
    assert equity == pytest.approx(100_000.0)
    assert as_of is None

    _seed_schwab(repo)
    equity, as_of = silo_equity_from_snapshots(repo, Silo.STOCK_OPTIONS, settings)
    assert equity == pytest.approx(125_000.5)
    assert as_of is not None


def test_current_positions_from_holdings(repo: Repository):
    _seed_schwab(repo)
    positions = current_positions(repo)
    assert len(positions) == 1
    assert positions[0].underlying == "AAPL"
    assert positions[0].status.value == "open"


def test_build_app_book_context_uses_snapshot_equity(repo: Repository):
    _seed_schwab(repo)
    settings = AppSettings(starting_equity_stock_options=100_000.0)
    book = build_app_book_context([], [], settings, repo=repo)
    assert book.stock_options.equity == pytest.approx(125_000.5)


def test_silo_peak_from_balance_history(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="bank", silo=Silo.STOCK_OPTIONS)
    settings = AppSettings(starting_equity_stock_options=100_000.0)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 5, 1, tzinfo=timezone.utc),
            cash=0,
            equity_value=130_000.0,
            buying_power=0,
            source="manual",
        )
    )
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=0,
            equity_value=120_000.0,
            buying_power=0,
            source="manual",
        )
    )
    peak = silo_peak_equity_from_snapshots(repo, Silo.STOCK_OPTIONS, settings)
    assert peak == pytest.approx(130_000.0)
