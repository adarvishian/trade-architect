"""Phase 2 — price targets, reward:risk, and option delta wiring."""

from __future__ import annotations

import pytest

from trading_architect.config.sizing import SizingConfig
from trading_architect.engines.sizing import (
    BindingConstraint,
    CandidateTrade,
    SiloExposure,
    _notional_per_unit,
    recommend_size,
)
from trading_architect.models.entities import AssetType, Direction, Silo


def test_stock_reward_risk_from_entry_stop_target():
    """Entry 100, stop 95, target 115 → R:R 3.0 (15 reward / 5 risk)."""
    rec = recommend_size(
        CandidateTrade(
            silo=Silo.STOCK_OPTIONS,
            underlying="AAPL",
            direction=Direction.LONG,
            asset_type=AssetType.STOCK,
            entry_price=100.0,
            stop_price=95.0,
            spot_price=100.0,
            target_prices=(115.0,),
        ),
        SiloExposure(silo_equity=100_000.0),
        config=SizingConfig(base_risk_f=0.01),
    )
    assert rec.reward_risk_ratio == pytest.approx(3.0)
    assert len(rec.target_analyses) == 1
    assert rec.target_analyses[0].r_multiple == pytest.approx(3.0)
    assert rec.recommended_qty == pytest.approx(200.0)
    assert rec.dollar_reward_at_target == pytest.approx(200.0 * 15.0)
    assert "Reward:risk" in rec.rationale
    assert "3.00" in rec.rationale or "3.0" in rec.rationale


def test_option_reward_risk_with_projected_premium():
    """BS/projected premium at target drives option R:R."""
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
            target_prices=(550.0,),
            projected_premium_at_targets=(10.0,),
        ),
        SiloExposure(silo_equity=100_000.0),
        config=SizingConfig(base_risk_f=0.01),
    )
    assert rec.reward_risk_ratio == pytest.approx(1.0)
    assert rec.target_analyses[0].r_multiple == pytest.approx(1.0)
    assert rec.option_delta_used == pytest.approx(0.45)
    assert "Option delta used" in rec.rationale


def test_option_non_atm_delta_affects_notional():
    """Supplied ITM delta (0.75) must not fall back to 0.5 for leverage math."""
    candidate = CandidateTrade(
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        direction=Direction.LONG,
        asset_type=AssetType.OPTION,
        entry_price=12.0,
        premium_per_contract=12.0,
        spot_price=180.0,
        option_delta=0.75,
        target_prices=(190.0,),
    )
    notional_075 = _notional_per_unit(candidate)
    candidate_default = CandidateTrade(
        silo=candidate.silo,
        underlying=candidate.underlying,
        direction=candidate.direction,
        asset_type=candidate.asset_type,
        entry_price=candidate.entry_price,
        premium_per_contract=candidate.premium_per_contract,
        spot_price=candidate.spot_price,
        target_prices=candidate.target_prices,
    )
    notional_default = _notional_per_unit(candidate_default)
    assert notional_075 == pytest.approx(0.75 * 100 * 180.0)
    assert notional_default == pytest.approx(0.5 * 100 * 180.0)
    assert notional_075 > notional_default

    rec = recommend_size(
        candidate,
        SiloExposure(silo_equity=50_000.0, open_delta_notional=95_000.0),
        config=SizingConfig(leverage_cap=2.0, base_risk_f=0.02),
    )
    assert rec.option_delta_used == pytest.approx(0.75)
    assert rec.binding_constraint == BindingConstraint.LEVERAGE_CAP


def test_option_delta_linear_approximation_when_no_projection():
    """Underlying target with delta approximates projected premium."""
    rec = recommend_size(
        CandidateTrade(
            silo=Silo.STOCK_OPTIONS,
            underlying="TSLA",
            direction=Direction.LONG,
            asset_type=AssetType.OPTION,
            entry_price=8.0,
            premium_per_contract=8.0,
            spot_price=250.0,
            option_delta=0.40,
            target_prices=(260.0,),
        ),
        SiloExposure(silo_equity=100_000.0),
    )
    # projected = 8 + 0.4 * 10 = 12 → R = (12-8)/8 = 0.5
    assert rec.reward_risk_ratio == pytest.approx(0.5)


def test_multiple_targets_each_analyzed():
    rec = recommend_size(
        CandidateTrade(
            silo=Silo.STOCK_OPTIONS,
            underlying="MSFT",
            direction=Direction.LONG,
            asset_type=AssetType.STOCK,
            entry_price=400.0,
            stop_price=390.0,
            spot_price=400.0,
            target_prices=(420.0, 440.0),
        ),
        SiloExposure(silo_equity=100_000.0),
    )
    assert len(rec.target_analyses) == 2
    assert rec.target_analyses[0].reward_risk_ratio == pytest.approx(2.0)
    assert rec.target_analyses[1].reward_risk_ratio == pytest.approx(4.0)
    assert rec.reward_risk_ratio == pytest.approx(2.0)
