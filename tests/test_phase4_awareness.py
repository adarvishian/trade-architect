"""Phase 4 — edge & awareness: TWR, clusters, earnings, slippage, exit efficiency."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from trading_architect.assembly.entries import ClosedTradeEntry
from trading_architect.config.user_settings import AppSettings
from trading_architect.models.entities import (
    AssetType,
    Direction,
    Position,
    PositionStatus,
    Silo,
)
from trading_architect.services.benchmark import parse_spy_close_from_quotes
from trading_architect.services.dashboard_metrics import top_cluster_stress
from trading_architect.services.earnings import earnings_flags_for_positions
from trading_architect.services.exit_efficiency import exit_efficiency_report
from trading_architect.services.stop_slippage import (
    _exit_slippage_pct,
    slippage_calibration,
)
from trading_architect.services.twr import _chain_link_return, twr_report
from trading_architect.store.database import Database
from trading_architect.store.repository import (
    BalanceSnapshotRecord,
    BenchmarkPriceRecord,
    CashEventRecord,
    HoldingSnapshotRecord,
    Repository,
)


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "phase4.db"))


def _seed_account(repo: Repository, label: str = "schwab") -> int:
    acct = repo.upsert_account(kind="schwab", label=label, silo=Silo.STOCK_OPTIONS)
    return acct.id


def test_chain_link_return_neutralizes_deposit(repo: Repository):
    acct_id = _seed_account(repo)
    d0 = date(2026, 1, 1)
    d1 = date(2026, 1, 2)
    d2 = date(2026, 1, 3)
    for d, eq in ((d0, 100_000.0), (d1, 110_000.0), (d2, 115_000.0)):
        repo.record_balance_snapshot(
            BalanceSnapshotRecord(
                account_id=acct_id,
                as_of=datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc),
                cash=eq,
                equity_value=eq,
                buying_power=eq,
                source="api",
            )
        )
    repo.record_cash_event(
        CashEventRecord(
            account_id=acct_id,
            detected_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            prior_cash=100_000.0,
            new_cash=110_000.0,
            delta=10_000.0,
            classification="deposit",
        )
    )
    series = repo.daily_equity_series()
    flows = repo.daily_cash_flows()
    # Deposit day return should be 0% after cash-flow adjustment.
    twr_through_deposit = _chain_link_return(series[:2], flows)
    assert twr_through_deposit == pytest.approx(0.0)
    twr = _chain_link_return(series, flows)
    assert twr == pytest.approx(115_000 / 110_000 - 1)


def test_twr_insufficient_history(repo: Repository):
    acct_id = _seed_account(repo)
    d0 = date(2026, 5, 1)
    d1 = date(2026, 5, 15)
    for d, eq in ((d0, 100_000.0), (d1, 105_000.0)):
        repo.record_balance_snapshot(
            BalanceSnapshotRecord(
                account_id=acct_id,
                as_of=datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc),
                cash=eq,
                equity_value=eq,
                buying_power=eq,
                source="api",
            )
        )
    report = twr_report(repo, as_of=d1)
    assert report.combined_ytd.sufficient_history is False


def test_twr_with_spy_benchmark(repo: Repository):
    acct_id = _seed_account(repo)
    start = date(2025, 6, 1)
    end = date(2026, 6, 1)
    for i in range(366):
        d = start + timedelta(days=i)
        eq = 100_000.0 + i * 100.0
        repo.record_balance_snapshot(
            BalanceSnapshotRecord(
                account_id=acct_id,
                as_of=datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc),
                cash=eq,
                equity_value=eq,
                buying_power=eq,
                source="api",
            )
        )
    repo.record_benchmark_price(
        BenchmarkPriceRecord(symbol="SPY", price_date=start, close_price=400.0)
    )
    repo.record_benchmark_price(
        BenchmarkPriceRecord(symbol="SPY", price_date=end, close_price=440.0)
    )
    report = twr_report(repo, as_of=end)
    assert report.combined_t12.sufficient_history is True
    assert report.combined_t12.benchmark_twr == pytest.approx(0.10)


def test_parse_spy_close():
    data = {"SPY": {"quote": {"closePrice": 550.25}}}
    assert parse_spy_close_from_quotes(data) == pytest.approx(550.25)


def test_cluster_stress_reacts_to_positions(repo: Repository):
    settings = AppSettings(cluster_stress_pct=-0.15)
    pos_a = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        component_event_ids=[],
        blended_cost_basis={},
        current_delta_notional=80_000.0,
    )
    pos_b = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="AMD",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        component_event_ids=[],
        blended_cost_basis={},
        current_delta_notional=20_000.0,
    )
    repo.upsert_underlying_tag("NVDA", "Semis")
    repo.upsert_underlying_tag("AMD", "Semis")
    stress = top_cluster_stress(
        [pos_a, pos_b],
        repo,
        equity=100_000.0,
        stress_pct=settings.cluster_stress_pct,
    )
    assert stress is not None
    assert stress.cluster_label == "Semis"
    assert stress.cluster_pct == pytest.approx(1.0)
    assert stress.stress_gap_dollars == pytest.approx(-15_000.0)
    assert stress.stress_gap_equity_pct == pytest.approx(-0.15)


def test_earnings_flags_manual(repo: Repository):
    repo.upsert_earnings_date("NVDA", date.today() + timedelta(days=4), source="manual")
    pos = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        component_event_ids=[],
        blended_cost_basis={"NVDA_2026-09-18_C_110.0": 5.0},
        leg_net_qty={"NVDA_2026-09-18_C_110.0": 3},
    )
    flags = earnings_flags_for_positions(repo, [pos])
    assert len(flags) == 1
    assert flags[0].days_until == 4
    assert flags[0].option_legs_held == 1


def test_slippage_calibrating_until_ten_observations(repo: Repository):
    cal = slippage_calibration(repo)
    assert cal.status == "calibrating"
    assert all(v == 0.0 for v in cal.pads_by_asset_type.values())


def test_exit_slippage_pct_long():
    slip = _exit_slippage_pct(direction=Direction.LONG, exit_price=95.0, stop_price=100.0)
    assert slip == pytest.approx(0.05)


def test_exit_efficiency_empty(repo: Repository):
    report = exit_efficiency_report(repo)
    assert report.trade_count == 0
    assert report.median_mfe_capture_pct is None


def test_exit_efficiency_with_marks(repo: Repository):
    acct_id = _seed_account(repo)
    opened = datetime(2026, 3, 1, tzinfo=timezone.utc)
    closed = datetime(2026, 3, 10, tzinfo=timezone.utc)
    for i, mark in enumerate([100.0, 110.0, 105.0]):
        ts = opened + timedelta(days=i)
        repo.record_holdings_snapshots(
            acct_id,
            ts,
            [
                HoldingSnapshotRecord(
                    account_id=acct_id,
                    as_of=ts,
                    symbol="AAPL",
                    asset_type="stock",
                    qty=10.0,
                    mark=mark,
                    mtm_value=mark * 10,
                    cost_basis=100.0,
                )
            ],
        )
    entry = ClosedTradeEntry(
        entry_id="e1",
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        symbol="AAPL",
        asset_type=AssetType.STOCK,
        direction=Direction.LONG,
        epoch_id=None,
        opened_at=opened,
        closed_at=closed,
        quantity=10.0,
        entry_price=100.0,
        exit_price=105.0,
        initial_risk=500.0,
        risk_per_unit=5.0,
        realized_pnl=50.0,
        realized_r=1.0,
        open_event_id="o1",
        close_event_id="c1",
        risk_basis="stop",
    )
    from trading_architect.services import exit_efficiency as ee_mod

    original = ee_mod.extract_closed_entries
    ee_mod.extract_closed_entries = lambda events: [entry]
    try:
        report = exit_efficiency_report(repo, limit=5)
    finally:
        ee_mod.extract_closed_entries = original
    assert report.trade_count == 1
    assert report.rows[0].max_r_reached == pytest.approx(2.0)
    assert report.rows[0].mfe_capture_pct == pytest.approx(0.5)
