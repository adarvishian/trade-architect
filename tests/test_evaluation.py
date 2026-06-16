"""Tests for M2 evaluation engine."""

import warnings
from datetime import datetime

import pytest

from trading_architect.assembly.entries import ClosedTradeEntry, extract_closed_entries
from trading_architect.engines.evaluation import CounterfactualRule, _counterfactual_qty, evaluate_alpha_left
from trading_architect.engines.r_distribution import compute_r_distribution
from trading_architect.models.entities import AssetType, Direction, Side, Silo, TradeEvent


def _stock_round_trip(qty: float, entry: float, exit: float, stop: float) -> list[TradeEvent]:
    return [
        TradeEvent(
            broker="test",
            account="test",
            timestamp=datetime(2025, 1, 10),
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=qty,
            price=entry,
            stop_price=stop,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="open",
            epoch_id="pre-2026-03-27",
        ),
        TradeEvent(
            broker="test",
            account="test",
            timestamp=datetime(2025, 1, 20),
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.SELL,
            quantity=qty,
            price=exit,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="close",
            epoch_id="pre-2026-03-27",
        ),
    ]


def test_r_distribution_basic_stats():
    stats = compute_r_distribution([2.0, -1.0, 1.5, -0.5, 3.0])
    assert stats is not None
    assert stats.count == 5
    assert stats.win_rate == pytest.approx(0.6)
    assert stats.expectancy == pytest.approx(1.0)
    assert stats.optimal_f > 0


def test_r_distribution_negative_edge_returns_zero_optimal_f():
    stats = compute_r_distribution([-1.0, -0.5, -2.0, -1.5])
    assert stats is not None
    assert stats.optimal_f == 0.0


def test_r_distribution_constant_samples_no_skew_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        stats = compute_r_distribution([1.0, 1.0, 1.0, 1.0, 1.0])
    assert stats is not None
    assert stats.skew == 0.0


def test_optimal_f_floored_at_zero_for_ruinous_grid():
    """Growth-optimal f stays at 0 when every tested f produces ruin (1 + fR <= 0)."""
    stats = compute_r_distribution([-2.0, -3.0, -1.5, -2.5])
    assert stats is not None
    assert stats.optimal_f == 0.0


def test_alpha_left_positive_when_undersized():
    """Actual size below 1% fractional → positive alpha left."""
    events = _stock_round_trip(qty=10, entry=100, exit=110, stop=95)
    report = evaluate_alpha_left(
        events,
        starting_equity={Silo.STOCK_OPTIONS: 100_000.0, Silo.FUTURES: 50_000.0},
        rules=[CounterfactualRule.FRACTIONAL_1PCT],
    )
    seg = next(s for s in report.segments if s.rule == CounterfactualRule.FRACTIONAL_1PCT)
    # Actual risk $50; 1% of 100k = $1000 → could have sized 20x on risk-per-unit basis
    assert seg.alpha_left_on_table > 0
    assert seg.contributions[0].counterfactual_qty == pytest.approx(200.0, rel=0.01)


def test_counterfactual_preserves_entry_exit():
    events = _stock_round_trip(qty=5, entry=100, exit=120, stop=90)
    entries = extract_closed_entries(events)
    assert len(entries) == 1
    assert entries[0].realized_pnl == pytest.approx(100.0)

    report = evaluate_alpha_left(
        events,
        starting_equity={Silo.STOCK_OPTIONS: 50_000.0, Silo.FUTURES: 25_000.0},
        rules=[CounterfactualRule.FRACTIONAL_1PCT],
    )
    c = report.segments[0].contributions[0]
    # P&L scales linearly with qty — same entry/exit prices
    ratio = c.counterfactual_qty / c.actual_qty
    assert c.counterfactual_pnl == pytest.approx(c.actual_pnl * ratio)


def test_counterfactual_qty_skips_leverage_cap_when_entry_price_zero():
    """Zero entry price must not crash leverage-cap math."""
    entry = ClosedTradeEntry(
        entry_id="e1",
        silo=Silo.STOCK_OPTIONS,
        underlying="BAD",
        symbol="BAD",
        asset_type=AssetType.STOCK,
        direction=Direction.LONG,
        epoch_id="epoch",
        opened_at=datetime(2025, 1, 10),
        closed_at=datetime(2025, 1, 20),
        quantity=10.0,
        entry_price=0.0,
        exit_price=1.0,
        initial_risk=100.0,
        risk_per_unit=10.0,
        realized_pnl=10.0,
        realized_r=0.1,
        open_event_id="o1",
        close_event_id="c1",
        risk_basis="stop",
    )
    qty = _counterfactual_qty(entry, CounterfactualRule.FRACTIONAL_1PCT, 100_000.0, 0.1)
    assert qty >= 0.0


def test_evaluate_alpha_left_with_zero_entry_price():
    events = _stock_round_trip(qty=10, entry=0, exit=1, stop=0.5)
    report = evaluate_alpha_left(
        events,
        starting_equity={Silo.STOCK_OPTIONS: 100_000.0, Silo.FUTURES: 50_000.0},
        rules=[CounterfactualRule.FRACTIONAL_1PCT],
    )
    assert report.segments
