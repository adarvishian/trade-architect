"""Production wiring for attribution-aware drawdown governor (PRD Phase 5.2)."""

from __future__ import annotations

from trading_architect.config.defaults import DEFAULT_CORRELATION_THRESHOLD
from trading_architect.engines.correlation import correlation_provider_from_events
from trading_architect.engines.drawdown import (
    DrawdownAttribution,
    build_attribution,
    candidate_drawdown_correlation,
)
from trading_architect.engines.equity import open_pnl_by_underlying_for_silo
from trading_architect.engines.marks import MarksProvider, default_marks_provider
from trading_architect.models.entities import Position, Silo, TradeEvent


def drawdown_attribution_for_silo(
    positions: list[Position],
    events: list[TradeEvent],
    *,
    silo: Silo,
    marks_provider: MarksProvider | None = None,
    correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> DrawdownAttribution:
    """Cluster open underlyings and flag those driving the current drawdown."""
    marks_provider = marks_provider if marks_provider is not None else default_marks_provider()
    pnl_by_underlying = open_pnl_by_underlying_for_silo(
        events, positions, silo, marks_provider=marks_provider
    )
    silo_events = [e for e in events if e.silo == silo]
    provider = correlation_provider_from_events(silo_events)
    return build_attribution(pnl_by_underlying, provider, correlation_threshold)


def candidate_drawdown_correlation_for_sizing(
    candidate_underlying: str,
    positions: list[Position],
    events: list[TradeEvent],
    *,
    silo: Silo,
    marks_provider: MarksProvider | None = None,
    correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> float:
    """Correlation of a candidate to the cluster driving the current drawdown."""
    attribution = drawdown_attribution_for_silo(
        positions,
        events,
        silo=silo,
        marks_provider=marks_provider,
        correlation_threshold=correlation_threshold,
    )
    silo_events = [e for e in events if e.silo == silo]
    provider = correlation_provider_from_events(silo_events)
    return candidate_drawdown_correlation(
        candidate_underlying.upper(), attribution, provider
    )
