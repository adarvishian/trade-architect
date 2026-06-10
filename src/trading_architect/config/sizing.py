"""Sizing configuration — user-overridable per PRD §7.3 FR-3.8."""

from __future__ import annotations

from dataclasses import dataclass, field

from trading_architect.config.defaults import (
    DEFAULT_ASSUMED_CORRELATION,
    DEFAULT_BASE_RISK_F,
    DEFAULT_CORRELATION_THRESHOLD,
    DEFAULT_HEAT_CAP,
    DEFAULT_KELLY_FRACTION,
    DEFAULT_LEVERAGE_CAP,
    DRAWDOWN_HARD_CAP,
    DRAWDOWN_SOFT_ALERT,
    DRAWDOWN_THROTTLE_START,
)


@dataclass
class EquityTier:
    """Layer 5 — scale base risk when equity crosses tier boundaries."""

    min_equity: float
    f_multiplier: float
    label: str = ""


@dataclass
class SizingConfig:
    base_risk_f: float = DEFAULT_BASE_RISK_F
    kelly_fraction: float = DEFAULT_KELLY_FRACTION
    heat_cap: float = DEFAULT_HEAT_CAP
    leverage_cap: float = DEFAULT_LEVERAGE_CAP
    reference_atr: float | None = None
    use_kelly_lower_ci: bool = True
    bootstrap_samples: int = 500
    equity_tiers: list[EquityTier] = field(
        default_factory=lambda: [
            EquityTier(0, 1.0, "base"),
            EquityTier(150_000, 1.0, "150k+"),
            EquityTier(250_000, 1.1, "250k+"),
            EquityTier(500_000, 1.2, "500k+"),
        ]
    )
    drawdown_soft: float = DRAWDOWN_SOFT_ALERT
    drawdown_throttle_start: float = DRAWDOWN_THROTTLE_START
    drawdown_hard: float = DRAWDOWN_HARD_CAP
    # Attribution-aware governor: localize drawdown throttling by correlation.
    attribution_aware_governor: bool = True
    correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD
    assumed_correlation: float = DEFAULT_ASSUMED_CORRELATION


DEFAULT_SIZING_CONFIG = SizingConfig()
