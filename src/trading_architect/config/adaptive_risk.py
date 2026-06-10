"""Adaptive risk engine configuration — PRD §7.4 FR-4.x."""

from __future__ import annotations

from dataclasses import dataclass

from trading_architect.config.defaults import (
    DEFAULT_BASE_RISK_F,
    DEFAULT_KELLY_FRACTION,
    DRAWDOWN_HARD_CAP,
    DRAWDOWN_SOFT_ALERT,
    DRAWDOWN_THROTTLE_START,
)

# Kelly fraction ladder for step-up recommendations (FR-4.3)
KELLY_FRACTION_LADDER: tuple[float, ...] = (0.25, 1 / 3, 0.5, 0.67, 1.0)
KELLY_FRACTION_LABELS: dict[float, str] = {
    0.25: "¼ Kelly",
    1 / 3: "⅓ Kelly",
    0.5: "½ Kelly",
    0.67: "⅔ Kelly",
    1.0: "Full Kelly",
}


@dataclass
class AdaptiveRiskConfig:
    """User-overridable thresholds for risk-appetite recommendations."""

    current_base_f: float = DEFAULT_BASE_RISK_F
    current_kelly_fraction: float = DEFAULT_KELLY_FRACTION
    kelly_ladder: tuple[float, ...] = KELLY_FRACTION_LADDER

    min_trades_for_ci: int = 5
    min_trades_for_step_up: int = 15
    bootstrap_samples: int = 500
    ci_level: float = 0.05

    # Lower-CI optimal-f must clear this to recommend stepping Kelly or base f
    step_up_optimal_f_lower_threshold: float = 0.008
    step_up_expectancy_lower_threshold: float = 0.0

    # Base-f step ladder (fraction of equity per trade)
    base_f_ladder: tuple[float, ...] = (0.005, 0.01, 0.015, 0.02)

    drawdown_soft: float = DRAWDOWN_SOFT_ALERT
    drawdown_throttle_start: float = DRAWDOWN_THROTTLE_START
    drawdown_hard: float = DRAWDOWN_HARD_CAP


DEFAULT_ADAPTIVE_RISK_CONFIG = AdaptiveRiskConfig()
