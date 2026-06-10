"""Tests for M5 options contract selector."""

from datetime import datetime
from pathlib import Path

from trading_architect.config.options_selector import IvAssumption, OptionSelectorConfig
from trading_architect.engines.options_selector import (
    OptionDirection,
    OptionSelectorInput,
    enumerate_candidates,
    rank_contracts,
    reprice_at_target,
)
from trading_architect.engines.sizing import SiloExposure
from trading_architect.ingestion.chain import parse_chain_csv
from trading_architect.ingestion.schwab_rest import parse_option_chain_response
from trading_architect.models.entities import ChainContract, ChainSnapshot

SCHWAB_CHAIN_FIXTURE = Path(__file__).parent / "fixtures" / "schwab" / "option_chain_nvda.json"

FIXTURE = Path(__file__).parent / "fixtures" / "chain_sample.csv"


def _snapshot() -> ChainSnapshot:
    return parse_chain_csv(FIXTURE, "NVDA", spot_price=200.0, asof=datetime(2026, 5, 26))


def test_parse_chain_csv():
    snap = _snapshot()
    assert snap.underlying == "NVDA"
    assert len(snap.contracts) >= 5
    assert snap.contracts[0].strike > 0


def test_enumerate_call_candidates_near_target():
    snap = _snapshot()
    inputs = OptionSelectorInput(
        underlying="NVDA",
        direction=OptionDirection.LONG_CALL,
        target_price=210.0,
        expected_hold_days=60,
    )
    cfg = OptionSelectorConfig(min_dte=90, max_dte=250, dte_buffer_days=30)
    candidates = enumerate_candidates(snap, inputs, cfg)
    assert candidates
    strikes = {c.strike for c in candidates}
    assert max(strikes) <= 210.0


def test_reprice_at_target_increases_otm_call_value():
    snap = _snapshot()
    contract = next(c for c in snap.contracts if c.strike == 200)
    premium = contract.mid or 3.0
    projected, ret_pct, r_mult, _ = reprice_at_target(
        contract,
        snap,
        target_price=220.0,
        expected_hold_days=60,
        premium=premium,
        config=OptionSelectorConfig(),
    )
    assert projected > premium
    assert ret_pct > 0
    assert r_mult > 0


def test_iv_decline_lowers_projected_value():
    snap = _snapshot()
    contract = snap.contracts[0]
    premium = contract.mid or 5.0
    const_cfg = OptionSelectorConfig(iv_assumption=IvAssumption.CONSTANT)
    decline_cfg = OptionSelectorConfig(iv_assumption=IvAssumption.DECLINE, iv_decline_pct=0.20)
    p_const, _, _, _ = reprice_at_target(contract, snap, 220.0, 60, premium, const_cfg)
    p_decline, _, _, _ = reprice_at_target(contract, snap, 220.0, 60, premium, decline_cfg)
    assert p_decline < p_const


def test_rank_contracts_returns_ordered_shortlist():
    snap = _snapshot()
    result = rank_contracts(
        snap,
        OptionSelectorInput(
            underlying="NVDA",
            direction=OptionDirection.LONG_CALL,
            target_price=210.0,
            expected_hold_days=60,
        ),
        SiloExposure(silo_equity=100_000.0),
        top_n=3,
    )
    assert len(result.ranked) <= 3
    assert result.ranked
    scores = [r.composite_score for r in result.ranked]
    assert scores == sorted(scores, reverse=True)
    for item in result.ranked:
        assert item.size_recommendation is not None
        assert item.projected_r_multiple is not None
        assert "probability" not in item.rationale.lower()


def test_rank_schwab_chain_same_result_shape_as_csv():
    import json

    data = json.loads(SCHWAB_CHAIN_FIXTURE.read_text())
    snap = parse_option_chain_response(
        data,
        "NVDA",
        min_dte=90,
        max_dte=400,
        asof=datetime(2026, 5, 21),
    )
    result = rank_contracts(
        snap,
        OptionSelectorInput(
            underlying="NVDA",
            direction=OptionDirection.LONG_CALL,
            target_price=1000.0,
            expected_hold_days=60,
        ),
        SiloExposure(silo_equity=100_000.0),
        top_n=3,
    )
    assert result.snapshot.underlying == "NVDA"
    assert result.ranked
    scores = [r.composite_score for r in result.ranked]
    assert scores == sorted(scores, reverse=True)


def test_rank_empty_when_no_matching_right():
    snap = ChainSnapshot(
        underlying="X",
        asof_timestamp=datetime(2026, 5, 26),
        spot_price=100,
        contracts=[
            ChainContract(
                strike=100, expiry=datetime(2026, 9, 1).date(), dte=100, mid=2.0, right="C"
            )
        ],
    )
    result = rank_contracts(
        snap,
        OptionSelectorInput("X", OptionDirection.LONG_PUT, 90.0, 30),
        SiloExposure(50_000),
    )
    assert result.ranked == []
