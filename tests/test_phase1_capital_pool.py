"""Phase 1 — deployable capital pool wired into sizing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.book_context import build_book_context
from trading_architect.engines.capital import (
    CapitalBaseMode,
    resolve_capital_base,
    silo_deployable_capital,
)
from trading_architect.engines.sizing import (
    CandidateTrade,
    SiloExposure,
    recommend_size,
)
from trading_architect.models.entities import AssetType, Direction, Silo
from trading_architect.store.database import Database
from trading_architect.store.repository import BalanceSnapshotRecord, Repository


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "phase1.db"))


def _stock_candidate() -> CandidateTrade:
    return CandidateTrade(
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        direction=Direction.LONG,
        asset_type=AssetType.STOCK,
        entry_price=100.0,
        stop_price=95.0,
        spot_price=100.0,
    )


def _seed_manual_cash(repo: Repository, cash: float) -> None:
    acct = repo.upsert_account(
        kind="manual",
        label="bank",
        silo=Silo.STOCK_OPTIONS,
        include_in_deployable=True,
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


def test_deployable_capital_base_changes_recommended_size(repo: Repository):
    """Higher deployable pool → larger recommended size (1% / $5 risk per share)."""
    _seed_manual_cash(repo, 50_000.0)
    settings_low = AppSettings(
        monthly_expenses=5_000.0,
        cash_reserve_months=6,
        capital_base_mode=CapitalBaseMode.DEPLOYABLE.value,
    )
    settings_high = AppSettings(
        monthly_expenses=1_000.0,
        cash_reserve_months=1,
        capital_base_mode=CapitalBaseMode.DEPLOYABLE.value,
    )

    silo_equity = 100_000.0
    base_low = resolve_capital_base(repo, settings_low, Silo.STOCK_OPTIONS, silo_equity)
    base_high = resolve_capital_base(repo, settings_high, Silo.STOCK_OPTIONS, silo_equity)
    assert base_high.amount > base_low.amount

    rec_low = recommend_size(
        _stock_candidate(),
        SiloExposure(
            silo_equity=silo_equity,
            capital_base=base_low.amount,
            capital_base_detail=base_low.detail,
        ),
    )
    rec_high = recommend_size(
        _stock_candidate(),
        SiloExposure(
            silo_equity=silo_equity,
            capital_base=base_high.amount,
            capital_base_detail=base_high.detail,
        ),
    )
    assert rec_high.recommended_qty > rec_low.recommended_qty
    assert rec_low.capital_base == base_low.amount
    assert base_low.detail in rec_low.rationale


def test_silo_equity_mode_ignores_manual_cash(repo: Repository):
    _seed_manual_cash(repo, 80_000.0)
    settings = AppSettings(capital_base_mode=CapitalBaseMode.SILO_EQUITY.value)
    base = resolve_capital_base(repo, settings, Silo.STOCK_OPTIONS, 100_000.0)
    assert base.amount == pytest.approx(100_000.0)

    rec_deployable = recommend_size(
        _stock_candidate(),
        SiloExposure(silo_equity=100_000.0, capital_base=20_000.0),
    )
    rec_equity = recommend_size(
        _stock_candidate(),
        SiloExposure(silo_equity=100_000.0, capital_base=base.amount),
    )
    assert rec_equity.recommended_qty > rec_deployable.recommended_qty


def test_no_broker_session_uses_manual_balances(repo: Repository):
    """Manual balances alone supply deployable pool without a live broker."""
    _seed_manual_cash(repo, 40_000.0)
    settings = AppSettings(
        monthly_expenses=5_000.0,
        cash_reserve_months=6,
        capital_base_mode=CapitalBaseMode.DEPLOYABLE.value,
    )
    deployable = silo_deployable_capital(repo, settings, Silo.STOCK_OPTIONS)
    assert deployable.deployable_total == pytest.approx(10_000.0)

    base = resolve_capital_base(repo, settings, Silo.STOCK_OPTIONS, silo_equity=50_000.0)
    assert base.amount == pytest.approx(10_000.0)

    rec = recommend_size(
        _stock_candidate(),
        SiloExposure(
            silo_equity=50_000.0,
            capital_base=base.amount,
            capital_base_detail=base.detail,
        ),
    )
    assert rec.recommended_qty == pytest.approx(20.0)
    assert "deployable pool" in rec.rationale.lower()


def test_book_context_wires_capital_base(repo: Repository):
    _seed_manual_cash(repo, 30_000.0)
    settings = AppSettings(
        monthly_expenses=5_000.0,
        cash_reserve_months=6,
        capital_base_mode=CapitalBaseMode.DEPLOYABLE.value,
    )
    ctx = build_book_context([], [], settings, repo=repo)
    exposure = ctx.exposure_for_silo(Silo.STOCK_OPTIONS, repo=repo, settings=settings)
    assert exposure.capital_base is not None
    assert exposure.capital_base == pytest.approx(ctx.stock_options.equity)
    assert exposure.capital_base_detail is not None
    assert "deployable" in exposure.capital_base_detail.lower()


def test_forward_income_toggle_affects_deployable(repo: Repository):
    acct = repo.upsert_account(kind="schwab", label="schwab", silo=Silo.STOCK_OPTIONS)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=10_000.0,
            equity_value=60_000.0,
            buying_power=10_000.0,
            source="api",
        )
    )
    settings_off = AppSettings(
        monthly_income_after_tax=8_000.0,
        monthly_expenses=5_000.0,
        include_forward_income=False,
    )
    settings_on = AppSettings(
        monthly_income_after_tax=8_000.0,
        monthly_expenses=5_000.0,
        include_forward_income=True,
    )
    off = silo_deployable_capital(repo, settings_off, Silo.STOCK_OPTIONS)
    on = silo_deployable_capital(repo, settings_on, Silo.STOCK_OPTIONS)
    assert on.deployable_total == pytest.approx(off.deployable_total + 3_000.0)
