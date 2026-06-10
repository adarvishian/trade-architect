"""Drawdown governor — PRD §7.7 FR-7.1–7.4.

The governor is **attribution-aware**: drawdown is localized to the correlation
cluster(s) currently driving it, and new sizing is throttled only insofar as the
candidate opportunity is *correlated with that source*. A measurably uncorrelated
opportunity is sized on its own merits even while the book is underwater. The
20% hard cap remains an absolute, correlation-independent catastrophic backstop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from trading_architect.config.sizing import SizingConfig
from trading_architect.engines.correlation import (
    CorrelationProvider,
    cluster_underlyings,
    max_correlation_to_group,
)


class DrawdownGovernorState(str, Enum):
    NORMAL = "normal"
    SOFT_ALERT = "soft_alert"
    THROTTLE = "throttle"
    HARD = "hard"


@dataclass(frozen=True)
class DrawdownMetrics:
    current_equity: float
    peak_equity: float
    drawdown_pct: float
    state: DrawdownGovernorState
    throttle_multiplier: float
    message: str | None


def compute_drawdown_pct(current_equity: float, peak_equity: float) -> float:
    if peak_equity <= 0:
        return 0.0
    return max(0.0, (peak_equity - current_equity) / peak_equity)


def assess_drawdown_state(
    drawdown_pct: float,
    config: SizingConfig | None = None,
) -> DrawdownMetrics:
    """Map drawdown % to governor state and sizing throttle multiplier."""
    cfg = config or SizingConfig()

    if drawdown_pct < cfg.drawdown_soft:
        return DrawdownMetrics(
            current_equity=0.0,
            peak_equity=0.0,
            drawdown_pct=drawdown_pct,
            state=DrawdownGovernorState.NORMAL,
            throttle_multiplier=1.0,
            message=None,
        )

    if drawdown_pct < cfg.drawdown_throttle_start:
        return DrawdownMetrics(
            current_equity=0.0,
            peak_equity=0.0,
            drawdown_pct=drawdown_pct,
            state=DrawdownGovernorState.SOFT_ALERT,
            throttle_multiplier=0.85,
            message=f"Drawdown {drawdown_pct:.1%} — soft alert (≥{cfg.drawdown_soft:.0%})",
        )

    if drawdown_pct < cfg.drawdown_hard:
        ramp = (drawdown_pct - cfg.drawdown_throttle_start) / (
            cfg.drawdown_hard - cfg.drawdown_throttle_start
        )
        mult = max(0.25, 1.0 - 0.75 * ramp)
        return DrawdownMetrics(
            current_equity=0.0,
            peak_equity=0.0,
            drawdown_pct=drawdown_pct,
            state=DrawdownGovernorState.THROTTLE,
            throttle_multiplier=mult,
            message=(
                f"Drawdown {drawdown_pct:.1%} — de-risk ramp active ({mult:.0%} of normal size)"
            ),
        )

    return DrawdownMetrics(
        current_equity=0.0,
        peak_equity=0.0,
        drawdown_pct=drawdown_pct,
        state=DrawdownGovernorState.HARD,
        throttle_multiplier=0.0,
        message=f"Drawdown {drawdown_pct:.1%} — at or above hard cap ({cfg.drawdown_hard:.0%})",
    )


@dataclass(frozen=True)
class DrawdownAttribution:
    """Which correlation clusters are driving the current drawdown.

    ``clusters`` is the full set of effective-opportunity clusters among open
    underlyings; ``losing_underlyings`` are those in clusters with net-negative
    open P&L (the drawdown source we throttle against)."""

    clusters: list[set[str]] = field(default_factory=list)
    losing_underlyings: frozenset[str] = frozenset()

    @property
    def has_attribution(self) -> bool:
        return bool(self.losing_underlyings)


def build_attribution(
    open_pnl_by_underlying: Mapping[str, float],
    provider: CorrelationProvider,
    threshold: float,
) -> DrawdownAttribution:
    """Cluster open underlyings and flag those in net-losing clusters.

    ``open_pnl_by_underlying`` is mark-to-market open P&L per underlying (negative
    = underwater). A cluster drives the drawdown when its aggregate open P&L < 0."""
    clusters = cluster_underlyings(open_pnl_by_underlying.keys(), provider, threshold)
    losing: set[str] = set()
    for cluster in clusters:
        agg = sum(open_pnl_by_underlying.get(name, 0.0) for name in cluster)
        if agg < 0:
            losing |= cluster
    return DrawdownAttribution(clusters=clusters, losing_underlyings=frozenset(losing))


def candidate_drawdown_correlation(
    underlying: str,
    attribution: DrawdownAttribution,
    provider: CorrelationProvider,
) -> float:
    """Candidate's correlation to the drawdown source.

    1.0 when the candidate IS / strongly tracks the losing cluster (full throttle);
    ~0 when uncorrelated (no throttle). When the drawdown can't be attributed to any
    open losing cluster, returns 1.0 — conservative: we can't prove independence, so
    we don't relax the governor."""
    if not attribution.has_attribution:
        return 1.0
    return max(0.0, max_correlation_to_group(underlying, attribution.losing_underlyings, provider))


def attribution_aware_throttle(
    drawdown_pct: float,
    drawdown_correlation: float,
    config: SizingConfig | None = None,
) -> DrawdownMetrics:
    """Map (drawdown, candidate-correlation-to-source) to a sizing throttle.

    Relaxes the uniform band multiplier toward 1.0 as the candidate's correlation
    to the drawdown source falls. The hard cap is absolute regardless of correlation.
    With ``drawdown_correlation = 1.0`` this is identical to the uniform governor."""
    cfg = config or SizingConfig()
    uniform = assess_drawdown_state(drawdown_pct, cfg)

    # Hard cap and below-soft are correlation-independent.
    if uniform.state in (DrawdownGovernorState.NORMAL, DrawdownGovernorState.HARD):
        return uniform

    corr = min(1.0, max(0.0, drawdown_correlation))
    base = uniform.throttle_multiplier
    mult = base + (1.0 - base) * (1.0 - corr)
    mult = min(1.0, max(base, mult))

    if mult >= 1.0:
        message = (
            f"Drawdown {drawdown_pct:.1%}, but candidate is uncorrelated with the "
            f"drawdown source (corr {corr:.2f}) — sized at full edge"
        )
    else:
        message = (
            f"Drawdown {drawdown_pct:.1%}; candidate corr to source {corr:.2f} "
            f"→ {mult:.0%} of normal size"
        )
    return DrawdownMetrics(
        current_equity=0.0,
        peak_equity=0.0,
        drawdown_pct=drawdown_pct,
        state=uniform.state,
        throttle_multiplier=mult,
        message=message,
    )


def worst_governor_state(states: list[DrawdownGovernorState]) -> DrawdownGovernorState:
    order = [
        DrawdownGovernorState.NORMAL,
        DrawdownGovernorState.SOFT_ALERT,
        DrawdownGovernorState.THROTTLE,
        DrawdownGovernorState.HARD,
    ]
    worst = DrawdownGovernorState.NORMAL
    for state in states:
        if order.index(state) > order.index(worst):
            worst = state
    return worst
