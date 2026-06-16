"""PRD Phase 5 — attribution-aware drawdown throttle in production paths."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_architect.config.sizing import SizingConfig
from trading_architect.engines.correlation import returns_from_events
from trading_architect.engines.equity import open_pnl_by_underlying
from trading_architect.engines.marks import Mark, StaticMarksProvider
from trading_architect.engines.sizing import CandidateTrade, SiloExposure, recommend_size
from trading_architect.models.entities import (
    AssetType,
    Direction,
    Position,
    PositionStatus,
    Side,
    Silo,
    TradeEvent,
)
from trading_architect.services.drawdown_attribution import (
    candidate_drawdown_correlation_for_sizing,
    drawdown_attribution_for_silo,
)


def _stock_event(
    underlying: str,
    price: float,
    day_offset: int,
    *,
    silo: Silo = Silo.STOCK_OPTIONS,
) -> TradeEvent:
    return TradeEvent(
        broker="test",
        account="a",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day_offset),
        symbol=underlying,
        underlying=underlying,
        asset_type=AssetType.STOCK,
        side=Side.BUY,
        quantity=1.0,
        price=price,
        silo=silo,
        raw_ref=f"{underlying}-{day_offset}",
    )


def _correlated_price_events() -> list[TradeEvent]:
    """Tech names move together; GLD moves opposite (25 daily observations)."""
    events: list[TradeEvent] = []
    tech = {"AAPL": 100.0, "MSFT": 200.0, "NVDA": 400.0}
    gld = 180.0
    for day in range(25):
        tech_up = day % 2 == 0
        for sym, price in tech.items():
            tech[sym] = price * (1.01 if tech_up else 0.99)
            events.append(_stock_event(sym, tech[sym], day))
        gld *= 0.99 if tech_up else 1.01
        events.append(_stock_event("GLD", gld, day))
    return events


def _open_stock_position(underlying: str, qty: float, cost: float) -> Position:
    return Position(
        silo=Silo.STOCK_OPTIONS,
        underlying=underlying,
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={underlying: qty},
        blended_cost_basis={underlying: cost},
    )


class StaticMarks(StaticMarksProvider):
    def __init__(self, prices: dict[str, float]):
        super().__init__(
            {
                sym: Mark(symbol=sym, price=price, delta=0.5, asof=datetime.now(timezone.utc))
                for sym, price in prices.items()
            }
        )


def test_open_pnl_by_underlying_aggregates_legs():
    positions = [
        _open_stock_position("AAPL", 10.0, 110.0),
        _open_stock_position("MSFT", 5.0, 210.0),
    ]
    marks = {"AAPL": 100.0, "MSFT": 200.0}
    pnl = open_pnl_by_underlying(positions, marks, [])
    assert pnl["AAPL"] == pytest.approx(-100.0)
    assert pnl["MSFT"] == pytest.approx(-50.0)


def test_returns_from_events_builds_daily_series():
    events = _correlated_price_events()
    returns = returns_from_events(events)
    assert len(returns["AAPL"]) == 24
    assert returns["AAPL"] == pytest.approx(returns["MSFT"], rel=1e-6)


def test_drawdown_attribution_flags_losing_tech_cluster():
    events = _correlated_price_events()
    positions = [
        _open_stock_position("AAPL", 10.0, 110.0),
        _open_stock_position("MSFT", 5.0, 210.0),
        _open_stock_position("GLD", 20.0, 170.0),
    ]
    marks = StaticMarks({"AAPL": 100.0, "MSFT": 200.0, "GLD": 180.0})
    attribution = drawdown_attribution_for_silo(
        positions,
        events,
        silo=Silo.STOCK_OPTIONS,
        marks_provider=marks,
    )
    assert "AAPL" in attribution.losing_underlyings
    assert "MSFT" in attribution.losing_underlyings
    assert "GLD" not in attribution.losing_underlyings


def test_candidate_correlation_lower_for_uncorrelated_name():
    events = _correlated_price_events()
    positions = [
        _open_stock_position("AAPL", 10.0, 110.0),
        _open_stock_position("MSFT", 5.0, 210.0),
    ]
    marks = StaticMarks({"AAPL": 100.0, "MSFT": 200.0})
    gld_corr = candidate_drawdown_correlation_for_sizing(
        "GLD",
        positions,
        events,
        silo=Silo.STOCK_OPTIONS,
        marks_provider=marks,
    )
    nvda_corr = candidate_drawdown_correlation_for_sizing(
        "NVDA",
        positions,
        events,
        silo=Silo.STOCK_OPTIONS,
        marks_provider=marks,
    )
    assert gld_corr < nvda_corr
    assert gld_corr < 0.2
    assert nvda_corr > 0.5


def test_production_sizing_relaxes_throttle_for_uncorrelated_candidate():
    """End-to-end: GLD-sized larger than NVDA when tech cluster drives drawdown."""
    events = _correlated_price_events()
    positions = [
        _open_stock_position("AAPL", 10.0, 110.0),
        _open_stock_position("MSFT", 5.0, 210.0),
    ]
    marks = StaticMarks({"AAPL": 100.0, "MSFT": 200.0})
    cfg = SizingConfig(base_risk_f=0.01, reference_atr=None)
    exposure = SiloExposure(silo_equity=100_000.0, drawdown_pct=0.17)

    def _candidate(underlying: str) -> CandidateTrade:
        return CandidateTrade(
            silo=Silo.STOCK_OPTIONS,
            underlying=underlying,
            direction=Direction.LONG,
            asset_type=AssetType.STOCK,
            entry_price=100.0,
            stop_price=95.0,
            spot_price=100.0,
        )

    gld_corr = candidate_drawdown_correlation_for_sizing(
        "GLD", positions, events, silo=Silo.STOCK_OPTIONS, marks_provider=marks
    )
    nvda_corr = candidate_drawdown_correlation_for_sizing(
        "NVDA", positions, events, silo=Silo.STOCK_OPTIONS, marks_provider=marks
    )
    gld_rec = recommend_size(
        _candidate("GLD"),
        exposure,
        config=cfg,
        drawdown_correlation=gld_corr,
    )
    nvda_rec = recommend_size(
        _candidate("NVDA"),
        exposure,
        config=cfg,
        drawdown_correlation=nvda_corr,
    )
    assert nvda_rec.recommended_qty < gld_rec.recommended_qty
    assert gld_rec.recommended_qty == pytest.approx(200.0)
    assert "uncorrelated" in gld_rec.rationale.lower()
