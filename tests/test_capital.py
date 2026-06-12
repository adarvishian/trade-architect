"""Phase 2 — deployable capital and projection."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.capital import (
    deployable_capital,
    monthly_net_cashflow,
    project_equity,
    reserve,
    silo_brokerage_liquidity,
)
from trading_architect.models.entities import Silo
from trading_architect.store.database import Database
from trading_architect.store.repository import BalanceSnapshotRecord, Repository


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "capital_test.db"))


def _manual_balance(repo: Repository, label: str, cash: float, *, deployable: bool = True):
    acct = repo.upsert_account(
        kind="manual",
        label=label,
        silo=Silo.STOCK_OPTIONS,
        include_in_deployable=deployable,
    )
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=cash,
            equity_value=cash,
            buying_power=cash,
            source="manual",
        )
    )
    return acct


def _broker_balance(repo: Repository, label: str, cash: float, silo=Silo.STOCK_OPTIONS):
    acct = repo.upsert_account(kind="schwab", label=label, silo=silo)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=cash,
            equity_value=cash + 50_000,
            buying_power=cash * 2,
            source="api",
        )
    )
    return acct


def test_monthly_net_and_reserve():
    settings = AppSettings(
        monthly_income_after_tax=10_000.0,
        monthly_expenses=6_000.0,
        cash_reserve_months=6,
    )
    assert monthly_net_cashflow(settings) == pytest.approx(4_000.0)
    assert reserve(settings) == pytest.approx(36_000.0)


def test_deployable_with_brokerage_and_transferable(repo: Repository):
    _broker_balance(repo, "schwab-tos", 5_000.0)
    _manual_balance(repo, "bank-checking", 50_000.0)
    settings = AppSettings(
        monthly_income_after_tax=8_000.0,
        monthly_expenses=5_000.0,
        cash_reserve_months=6,
    )
    breakdown = deployable_capital(repo, settings)
    assert breakdown.brokerage_cash == pytest.approx(5_000.0)
    assert breakdown.reserve_held == pytest.approx(30_000.0)
    assert breakdown.transferable_cash == pytest.approx(20_000.0)
    assert breakdown.monthly_net_cashflow == pytest.approx(3_000.0)
    assert breakdown.deployable_total == pytest.approx(28_000.0)
    assert breakdown.total_capital == pytest.approx(105_000.0)


def test_negative_net_cashflow(repo: Repository):
    _broker_balance(repo, "schwab-tos", 2_000.0)
    settings = AppSettings(
        monthly_income_after_tax=3_000.0,
        monthly_expenses=5_000.0,
        cash_reserve_months=6,
    )
    breakdown = deployable_capital(repo, settings)
    assert breakdown.monthly_net_cashflow == pytest.approx(-2_000.0)
    assert breakdown.deployable_total == pytest.approx(0.0)


def test_zero_accounts(repo: Repository):
    settings = AppSettings()
    breakdown = deployable_capital(repo, settings)
    assert breakdown.total_capital == 0.0
    assert breakdown.deployable_total == 0.0


def test_reserve_exceeds_manual_cash(repo: Repository):
    _manual_balance(repo, "bank", 10_000.0)
    settings = AppSettings(monthly_expenses=5_000.0, cash_reserve_months=6)
    breakdown = deployable_capital(repo, settings)
    assert breakdown.transferable_cash == 0.0
    assert breakdown.reserve_held == pytest.approx(30_000.0)


def test_project_equity_compound_and_contribution():
    series = project_equity(100_000.0, 1_000.0, 0.12, 12)
    assert len(series) == 13
    assert series[0] == pytest.approx(100_000.0)
    assert series[-1] > series[0]


def test_silo_brokerage_liquidity(repo: Repository):
    _broker_balance(repo, "schwab-tos", 3_000.0)
    _manual_balance(repo, "bank", 20_000.0)
    cash, bp = silo_brokerage_liquidity(repo, Silo.STOCK_OPTIONS)
    assert cash == pytest.approx(6_000.0)
    assert bp == pytest.approx(6_000.0)


def test_silo_brokerage_liquidity_missing_data(repo: Repository):
    cash, bp = silo_brokerage_liquidity(repo, Silo.FUTURES)
    assert cash is None
    assert bp is None


def test_futures_silo_manual_balance(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="tradovate", silo=Silo.FUTURES)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=25_000.0,
            equity_value=25_000.0,
            buying_power=25_000.0,
            source="manual",
        )
    )
    cash, _ = silo_brokerage_liquidity(repo, Silo.FUTURES)
    assert cash == pytest.approx(25_000.0)
