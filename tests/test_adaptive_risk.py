"""Tests for M4 adaptive risk & Kelly engine."""

from datetime import datetime, timedelta

import pytest

from trading_architect.config.adaptive_risk import AdaptiveRiskConfig
from trading_architect.config.sizing import SizingConfig
from trading_architect.engines.adaptive_risk import (
    RiskAction,
    build_risk_review,
    compute_edge_estimate,
    recommend_risk_appetite,
)
from trading_architect.engines.r_distribution import bootstrap_resample_stats
from trading_architect.models.entities import AssetType, Side, Silo, TradeEvent


def _closed_trade(
    *,
    silo: Silo = Silo.STOCK_OPTIONS,
    epoch_id: str = "post-2026-03-27",
    entry: float = 100.0,
    exit: float = 110.0,
    stop: float = 95.0,
    qty: float = 10.0,
    offset_days: int = 0,
) -> list[TradeEvent]:
    opened = datetime(2026, 4, 1) + timedelta(days=offset_days)
    closed = opened + timedelta(days=5)
    tag = offset_days
    return [
        TradeEvent(
            broker="t",
            account="t",
            timestamp=opened,
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=qty,
            price=entry,
            stop_price=stop,
            silo=silo,
            raw_ref=f"o{tag}",
            epoch_id=epoch_id,
        ),
        TradeEvent(
            broker="t",
            account="t",
            timestamp=closed,
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.SELL,
            quantity=qty,
            price=exit,
            silo=silo,
            raw_ref=f"c{tag}",
            epoch_id=epoch_id,
        ),
    ]


def _winning_history(n: int = 20) -> list[TradeEvent]:
    events: list[TradeEvent] = []
    for i in range(n):
        events.extend(_closed_trade(offset_days=i * 7, exit=115.0))
    return events


def test_bootstrap_returns_expectancy_and_optimal_f_ci():
    r_vals = [2.0, -1.0, 1.5, -0.5, 3.0, 0.5, 1.0, -0.8, 2.5, 0.2]
    exp_ci, opt_ci = bootstrap_resample_stats(r_vals, seed=42)
    assert exp_ci is not None
    assert opt_ci is not None
    assert exp_ci.lower <= exp_ci.point <= exp_ci.upper
    assert opt_ci.lower <= opt_ci.point <= opt_ci.upper


def test_insufficient_data_withholds_step_up():
    events = _closed_trade(offset_days=0)
    rec = recommend_risk_appetite(
        events,
        Silo.STOCK_OPTIONS,
        config=AdaptiveRiskConfig(min_trades_for_ci=5, min_trades_for_step_up=15),
    )
    assert rec.action == RiskAction.INSUFFICIENT_DATA
    assert rec.edge.trades_needed_for_ci > 0
    assert "Hold current settings" in rec.narrative or "Hold" in rec.narrative


def test_drawdown_hard_cap_blocks_step_up():
    events = _winning_history(20)
    rec = recommend_risk_appetite(
        events,
        Silo.STOCK_OPTIONS,
        config=AdaptiveRiskConfig(
            min_trades_for_step_up=10, step_up_optimal_f_lower_threshold=0.001
        ),
        drawdown_pct=0.21,
    )
    assert rec.action == RiskAction.DE_RISK
    assert rec.drawdown_multiplier == 0.0


def test_step_up_kelly_when_edge_clears_threshold():
    events = _winning_history(25)
    rec = recommend_risk_appetite(
        events,
        Silo.STOCK_OPTIONS,
        config=AdaptiveRiskConfig(
            min_trades_for_step_up=10,
            step_up_optimal_f_lower_threshold=0.001,
            step_up_expectancy_lower_threshold=-1.0,
        ),
        sizing=SizingConfig(kelly_fraction=0.25),
    )
    assert rec.edge.sufficient_for_step_up
    assert rec.action in (RiskAction.STEP_UP_KELLY, RiskAction.HOLD)
    if rec.action == RiskAction.STEP_UP_KELLY:
        assert rec.recommended_kelly_fraction == pytest.approx(1 / 3)


def test_compute_edge_estimate_per_epoch():
    edge = compute_edge_estimate(
        [2.0, -1.0, 1.5, -0.5, 3.0, 0.5],
        silo=Silo.FUTURES,
        epoch_id="post-2026-03-27",
    )
    assert edge.trade_count == 6
    assert edge.r_distribution is not None
    assert edge.expectancy_ci is not None


def test_build_risk_review_includes_both_silos():
    events = _winning_history(12)
    report = build_risk_review(events)
    assert len(report.recommendations) == 2
    assert report.post_epoch_id == "post-2026-03-27"


def test_hold_when_sample_thin_but_some_trades():
    events = _winning_history(8)
    rec = recommend_risk_appetite(
        events,
        Silo.STOCK_OPTIONS,
        config=AdaptiveRiskConfig(min_trades_for_ci=5, min_trades_for_step_up=15),
    )
    assert rec.action == RiskAction.HOLD
    assert rec.withhold_reason is not None
    assert "more closed trades" in rec.withhold_reason.lower()
