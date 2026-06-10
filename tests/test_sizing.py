"""Tests for M3 six-layer sizing engine."""

from datetime import datetime

import pytest

from trading_architect.config.sizing import SizingConfig
from trading_architect.engines.sizing import (
    BindingConstraint,
    CandidateTrade,
    SiloExposure,
    aggregate_open_exposure,
    recommend_size,
)
from trading_architect.models.entities import (
    AssetType,
    Direction,
    Position,
    PositionStatus,
    Side,
    Silo,
    TradeEvent,
)


def _stock_candidate(
    entry: float = 100.0,
    stop: float = 95.0,
    atr: float | None = None,
) -> CandidateTrade:
    return CandidateTrade(
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        direction=Direction.LONG,
        asset_type=AssetType.STOCK,
        entry_price=entry,
        stop_price=stop,
        spot_price=entry,
        atr=atr,
    )


def test_fractional_risk_layer_stock():
    """1% of 100k / $5 risk per share → 200 shares."""
    rec = recommend_size(
        _stock_candidate(),
        SiloExposure(silo_equity=100_000.0),
        config=SizingConfig(base_risk_f=0.01, reference_atr=None),
    )
    assert rec.recommended_qty == pytest.approx(200.0)
    assert rec.dollar_risk == pytest.approx(1_000.0)
    assert rec.binding_constraint == BindingConstraint.FRACTIONAL_RISK


def test_heat_cap_binding():
    """Open risk near cap → new size throttled by heat."""
    exposure = SiloExposure(silo_equity=100_000.0, open_dollar_risk=9_500.0)
    rec = recommend_size(
        _stock_candidate(),
        exposure,
        config=SizingConfig(base_risk_f=0.01, heat_cap=0.10),
    )
    # Only ~$500 heat budget left → 100 shares at $5/share risk
    assert rec.recommended_qty == pytest.approx(100.0)
    assert rec.binding_constraint == BindingConstraint.HEAT_CAP


def test_leverage_cap_binding_options():
    rec = recommend_size(
        CandidateTrade(
            silo=Silo.STOCK_OPTIONS,
            underlying="NVDA",
            direction=Direction.LONG,
            asset_type=AssetType.OPTION,
            entry_price=5.0,
            premium_per_contract=5.0,
            spot_price=500.0,
            option_delta=0.45,
        ),
        SiloExposure(silo_equity=50_000.0, open_delta_notional=95_000.0),
        config=SizingConfig(leverage_cap=2.0, base_risk_f=0.02),
    )
    assert rec.recommended_qty < 20
    assert rec.binding_constraint == BindingConstraint.LEVERAGE_CAP


def test_drawdown_hard_suspends_sizing():
    rec = recommend_size(
        _stock_candidate(),
        SiloExposure(silo_equity=100_000.0, drawdown_pct=0.21),
    )
    assert rec.recommended_qty == 0.0
    assert rec.binding_constraint == BindingConstraint.DRAWDOWN_THROTTLE


def test_volatility_normalization_reduces_size():
    cfg = SizingConfig(base_risk_f=0.01, reference_atr=2.0)
    base = recommend_size(
        _stock_candidate(atr=4.0),
        SiloExposure(silo_equity=100_000.0),
        config=cfg,
    )
    assert base.recommended_qty == pytest.approx(100.0)  # 200 × (2/4)


def test_kelly_layer_from_history():
    events = [
        TradeEvent(
            broker="t",
            account="t",
            timestamp=datetime(2025, 6, 1),
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=10,
            price=100,
            stop_price=95,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="o1",
            epoch_id="post-2026-03-27",
        ),
        TradeEvent(
            broker="t",
            account="t",
            timestamp=datetime(2025, 6, 10),
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.SELL,
            quantity=10,
            price=110,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="c1",
            epoch_id="post-2026-03-27",
        ),
    ]
    rec = recommend_size(
        _stock_candidate(),
        SiloExposure(silo_equity=100_000.0),
        events=events,
    )
    assert any(layer.layer == 4 for layer in rec.layers)


def test_aggregate_open_exposure():
    positions = [
        Position(
            silo=Silo.STOCK_OPTIONS,
            underlying="AAPL",
            direction=Direction.LONG,
            status=PositionStatus.OPEN,
            total_dollar_risk=500.0,
            current_delta_notional=10_000.0,
        ),
    ]
    exp = aggregate_open_exposure(positions, Silo.STOCK_OPTIONS, 100_000.0)
    assert exp.open_dollar_risk == 500.0
    assert exp.open_delta_notional == 10_000.0


def test_option_premium_risk_basis():
    rec = recommend_size(
        CandidateTrade(
            silo=Silo.STOCK_OPTIONS,
            underlying="TSLA",
            direction=Direction.LONG,
            asset_type=AssetType.OPTION,
            entry_price=8.0,
            premium_per_contract=8.0,
            spot_price=250.0,
            option_delta=0.35,
        ),
        SiloExposure(silo_equity=100_000.0),
        config=SizingConfig(base_risk_f=0.01),
    )
    # $1000 risk / $800 per contract = 1.25 contracts
    assert rec.recommended_qty == pytest.approx(1.25, rel=0.01)
