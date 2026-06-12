"""Options contract selector configuration — PRD §7.5 FR-5.4."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IvAssumption(str, Enum):
    CONSTANT = "constant"
    DECLINE = "decline"


@dataclass
class ScoringWeights:
    """User-weighted ranking dimensions (FR-5.4)."""

    cost_efficiency: float = 0.20
    projected_payoff: float = 0.25
    theta_drag: float = 0.15
    vega_iv_risk: float = 0.15
    delta_responsiveness: float = 0.15
    breakeven_cushion: float = 0.10

    def normalized(self) -> ScoringWeights:
        total = (
            self.cost_efficiency
            + self.projected_payoff
            + self.theta_drag
            + self.vega_iv_risk
            + self.delta_responsiveness
            + self.breakeven_cushion
        )
        if total <= 0:
            return ScoringWeights()
        return ScoringWeights(
            cost_efficiency=self.cost_efficiency / total,
            projected_payoff=self.projected_payoff / total,
            theta_drag=self.theta_drag / total,
            vega_iv_risk=self.vega_iv_risk / total,
            delta_responsiveness=self.delta_responsiveness / total,
            breakeven_cushion=self.breakeven_cushion / total,
        )


@dataclass
class OptionSelectorConfig:
    """DTE band, strike heuristic, IV modeling, scoring."""

    min_dte: int = 90
    max_dte: int = 365
    dte_buffer_days: int = 30
    strikes_below_target: int = 2
    iv_assumption: IvAssumption = IvAssumption.CONSTANT
    iv_decline_pct: float = 0.10
    target_delta: float = 0.40
    scoring_weights: ScoringWeights = field(default_factory=ScoringWeights)


DEFAULT_OPTION_SELECTOR_CONFIG = OptionSelectorConfig()
