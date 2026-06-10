"""Tests for opportunity correlation + attribution-aware drawdown governor.

Design intent (PRD §7.3/§7.7): drawdown is localized to the correlation cluster
driving it; a measurably uncorrelated new opportunity is sized at full edge while
the book is underwater, but the 20% hard cap remains an absolute backstop. The
conservative default (no attribution supplied) must reproduce the uniform governor.
"""

import pytest

from trading_architect.config.sizing import SizingConfig
from trading_architect.engines.correlation import (
    ConservativeCorrelationProvider,
    ReturnsCorrelationProvider,
    cluster_underlyings,
    max_correlation_to_group,
)
from trading_architect.engines.drawdown import (
    DrawdownGovernorState,
    assess_drawdown_state,
    attribution_aware_throttle,
    build_attribution,
    candidate_drawdown_correlation,
)
from trading_architect.engines.sizing import CandidateTrade, SiloExposure, recommend_size
from trading_architect.models.entities import AssetType, Direction, Silo

# ---- correlation providers ------------------------------------------------


def test_conservative_provider_assumes_correlated():
    p = ConservativeCorrelationProvider(default_correlation=0.6)
    assert p.correlation("AAPL", "AAPL") == 1.0
    assert p.correlation("AAPL", "MSFT") == 0.6


def test_returns_provider_measures_correlation():
    up = [
        0.01,
        0.02,
        -0.01,
        0.03,
        -0.02,
        0.015,
        0.005,
        -0.005,
        0.02,
        0.01,
        0.01,
        -0.01,
        0.02,
        0.0,
        0.03,
        -0.02,
        0.01,
        0.01,
        -0.01,
        0.02,
    ]
    same = list(up)
    opposite = [-x for x in up]
    p = ReturnsCorrelationProvider({"A": up, "B": same, "C": opposite}, min_observations=20)
    assert p.correlation("A", "B") == pytest.approx(1.0, abs=1e-6)
    assert p.correlation("A", "C") == pytest.approx(-1.0, abs=1e-6)


def test_returns_provider_falls_back_when_thin():
    p = ReturnsCorrelationProvider(
        {"A": [0.01, 0.02], "B": [0.01, 0.02]},
        default_correlation=0.6,
        min_observations=20,
    )
    # Too few observations → conservative fallback, not a spurious 1.0.
    assert p.correlation("A", "B") == 0.6
    # Missing series → fallback too.
    assert p.correlation("A", "Z") == 0.6


# ---- clustering -----------------------------------------------------------


def test_clustering_groups_correlated_separates_independent():
    # AAPL~MSFT correlated; GLD independent of both.
    class P:
        def correlation(self, a, b):
            if a == b:
                return 1.0
            pair = {a, b}
            if pair == {"AAPL", "MSFT"}:
                return 0.8
            return 0.1

    clusters = cluster_underlyings(["AAPL", "MSFT", "GLD"], P(), threshold=0.5)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]
    big = max(clusters, key=len)
    assert big == {"AAPL", "MSFT"}


def test_max_correlation_to_empty_group_is_zero():
    assert max_correlation_to_group("AAPL", [], ConservativeCorrelationProvider()) == 0.0


# ---- attribution ----------------------------------------------------------


def _two_cluster_provider():
    class P:
        def correlation(self, a, b):
            if a == b:
                return 1.0
            tech = {"AAPL", "MSFT", "NVDA"}
            if a in tech and b in tech:
                return 0.8
            return 0.05

    return P()


def test_attribution_flags_only_losing_cluster():
    provider = _two_cluster_provider()
    # Tech cluster underwater; gold position green.
    open_pnl = {"AAPL": -4000.0, "MSFT": -2000.0, "GLD": 3000.0}
    attribution = build_attribution(open_pnl, provider, threshold=0.5)
    assert "AAPL" in attribution.losing_underlyings
    assert "MSFT" in attribution.losing_underlyings
    assert "GLD" not in attribution.losing_underlyings


def test_candidate_correlation_to_source():
    provider = _two_cluster_provider()
    open_pnl = {"AAPL": -4000.0, "MSFT": -2000.0}
    attribution = build_attribution(open_pnl, provider, threshold=0.5)
    # NVDA is correlated with the losing tech cluster → high.
    assert candidate_drawdown_correlation("NVDA", attribution, provider) == pytest.approx(0.8)
    # GLD is independent of the losing cluster → near zero.
    assert candidate_drawdown_correlation("GLD", attribution, provider) == pytest.approx(0.05)


def test_no_attribution_is_conservative():
    provider = ConservativeCorrelationProvider()
    # No open losing positions to attribute the drawdown to → assume correlated.
    attribution = build_attribution({"AAPL": 500.0}, provider, threshold=0.5)
    assert not attribution.has_attribution
    assert candidate_drawdown_correlation("ZZZ", attribution, provider) == 1.0


# ---- attribution-aware throttle ------------------------------------------


def test_throttle_below_soft_is_full_size():
    m = attribution_aware_throttle(0.05, 0.0)
    assert m.throttle_multiplier == 1.0
    assert m.state == DrawdownGovernorState.NORMAL


def test_throttle_hard_cap_absolute_regardless_of_correlation():
    # Even a perfectly uncorrelated opportunity is suspended past the hard cap.
    m = attribution_aware_throttle(0.22, 0.0)
    assert m.throttle_multiplier == 0.0
    assert m.state == DrawdownGovernorState.HARD


def test_correlated_candidate_matches_uniform_governor():
    cfg = SizingConfig()
    for dd in (0.12, 0.17):
        uniform = assess_drawdown_state(dd, cfg)
        attributed = attribution_aware_throttle(dd, 1.0, cfg)
        assert attributed.throttle_multiplier == pytest.approx(uniform.throttle_multiplier)


def test_uncorrelated_candidate_relaxes_throttle():
    cfg = SizingConfig()
    # In the throttle band, an uncorrelated opportunity is sized at full edge.
    m = attribution_aware_throttle(0.17, 0.0, cfg)
    assert m.throttle_multiplier == pytest.approx(1.0)
    # Partial correlation → partial relief, between uniform and full.
    uniform = assess_drawdown_state(0.17, cfg).throttle_multiplier
    partial = attribution_aware_throttle(0.17, 0.5, cfg).throttle_multiplier
    assert uniform < partial < 1.0


# ---- end-to-end sizing integration ---------------------------------------


def _stock_candidate(underlying="AAPL"):
    return CandidateTrade(
        silo=Silo.STOCK_OPTIONS,
        underlying=underlying,
        direction=Direction.LONG,
        asset_type=AssetType.STOCK,
        entry_price=100.0,
        stop_price=95.0,
        spot_price=100.0,
    )


def test_sizing_uncorrelated_opportunity_not_taxed_in_drawdown():
    cfg = SizingConfig(base_risk_f=0.01, reference_atr=None)
    exposure = SiloExposure(silo_equity=100_000.0, drawdown_pct=0.17)
    correlated = recommend_size(_stock_candidate(), exposure, config=cfg, drawdown_correlation=1.0)
    uncorrelated = recommend_size(
        _stock_candidate(), exposure, config=cfg, drawdown_correlation=0.0
    )
    # Correlated entry is throttled; uncorrelated entry is sized at full edge.
    assert correlated.recommended_qty < uncorrelated.recommended_qty
    assert uncorrelated.recommended_qty == pytest.approx(200.0)


def test_sizing_hard_cap_suspends_even_uncorrelated():
    cfg = SizingConfig(base_risk_f=0.01, reference_atr=None)
    exposure = SiloExposure(silo_equity=100_000.0, drawdown_pct=0.21)
    rec = recommend_size(_stock_candidate(), exposure, config=cfg, drawdown_correlation=0.0)
    assert rec.recommended_qty == 0.0
