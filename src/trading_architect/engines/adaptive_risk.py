"""Adaptive risk & Kelly-fraction recommendation engine — PRD §7.4 (M4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from trading_architect.assembly.entries import extract_closed_entries
from trading_architect.config.adaptive_risk import (
    DEFAULT_ADAPTIVE_RISK_CONFIG,
    KELLY_FRACTION_LABELS,
    AdaptiveRiskConfig,
)
from trading_architect.config.defaults import DEFAULT_EPOCHS
from trading_architect.config.sizing import DEFAULT_SIZING_CONFIG, SizingConfig
from trading_architect.engines.r_distribution import (
    BootstrapCI,
    RDistributionStats,
    bootstrap_resample_stats,
    compute_r_distribution,
)
from trading_architect.models.entities import Silo, TradeEvent


class RiskAction(str, Enum):
    HOLD = "hold"
    STEP_UP_KELLY = "step_up_kelly"
    STEP_UP_BASE_F = "step_up_base_f"
    DE_RISK = "de_risk"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass
class EdgeEstimate:
    """Epoch-segmented edge with bootstrap confidence intervals (FR-4.1)."""

    silo: Silo
    epoch_id: str
    trade_count: int
    r_distribution: RDistributionStats | None
    expectancy_ci: BootstrapCI | None
    optimal_f_ci: BootstrapCI | None
    sufficient_for_ci: bool
    sufficient_for_step_up: bool
    trades_needed_for_ci: int
    trades_needed_for_step_up: int


@dataclass
class RiskAppetiteRecommendation:
    """Plain-English risk-appetite readout for one silo (FR-4.2–4.6)."""

    silo: Silo
    epoch_id: str
    action: RiskAction
    current_base_f: float
    recommended_base_f: float
    current_kelly_fraction: float
    recommended_kelly_fraction: float
    effective_risk_f: float
    drawdown_multiplier: float
    narrative: str
    statistics: str
    edge: EdgeEstimate
    withhold_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class RiskReviewReport:
    """Full edge & risk review across silos (FR-8.4)."""

    recommendations: list[RiskAppetiteRecommendation]
    epoch_segments: list[EdgeEstimate]
    post_epoch_id: str | None


def _kelly_label(fraction: float) -> str:
    for key, label in KELLY_FRACTION_LABELS.items():
        if abs(fraction - key) < 1e-6:
            return label
    return f"{fraction:.0%} Kelly"


def _next_ladder_step(current: float, ladder: tuple[float, ...]) -> float | None:
    for step in ladder:
        if step > current + 1e-9:
            return step
    return None


def _drawdown_multiplier(
    drawdown_pct: float, config: AdaptiveRiskConfig
) -> tuple[float, str | None]:
    """Cap risk appetite under drawdown governor (FR-4.6)."""
    if drawdown_pct < config.drawdown_soft:
        return 1.0, None
    if drawdown_pct < config.drawdown_throttle_start:
        return 0.85, f"Drawdown {drawdown_pct:.1%} — soft alert; risk appetite capped at 85%"
    if drawdown_pct < config.drawdown_hard:
        ramp = (drawdown_pct - config.drawdown_throttle_start) / (
            config.drawdown_hard - config.drawdown_throttle_start
        )
        mult = max(0.25, 1.0 - 0.75 * ramp)
        return (
            mult,
            f"Drawdown {drawdown_pct:.1%} — de-risk ramp; risk appetite capped at {mult:.0%}",
        )
    return 0.0, f"Drawdown {drawdown_pct:.1%} — at hard cap; step-ups suspended"


def _estimate_trades_needed(
    current_count: int,
    min_trades: int,
    lower_ci: float | None,
    threshold: float,
) -> int:
    """Quantify additional trades needed before a step-up (FR-4.4)."""
    count_gap = max(0, min_trades - current_count)
    if lower_ci is None:
        return count_gap
    if lower_ci >= threshold:
        return 0
    if lower_ci <= 0:
        return max(count_gap, min_trades)
    # Heuristic: CI width shrinks ~1/sqrt(n); scale sample to close the gap
    scale = (threshold / lower_ci) ** 2
    projected = int(current_count * scale) + 1
    return max(count_gap, projected - current_count)


def compute_edge_estimate(
    r_values: list[float],
    *,
    silo: Silo,
    epoch_id: str,
    config: AdaptiveRiskConfig | None = None,
) -> EdgeEstimate:
    """Bootstrap edge estimate for a silo/epoch segment."""
    cfg = config or DEFAULT_ADAPTIVE_RISK_CONFIG
    clean = [r for r in r_values if r is not None]
    trade_count = len(clean)
    r_dist = compute_r_distribution(clean)
    exp_ci, opt_ci = bootstrap_resample_stats(
        clean,
        n_bootstrap=cfg.bootstrap_samples,
        ci=cfg.ci_level,
    )

    sufficient_ci = trade_count >= cfg.min_trades_for_ci and exp_ci is not None
    sufficient_step = trade_count >= cfg.min_trades_for_step_up and opt_ci is not None

    lower_f = opt_ci.lower if opt_ci else None
    trades_for_ci = max(0, cfg.min_trades_for_ci - trade_count)
    trades_for_step = _estimate_trades_needed(
        trade_count,
        cfg.min_trades_for_step_up,
        lower_f,
        cfg.step_up_optimal_f_lower_threshold,
    )

    return EdgeEstimate(
        silo=silo,
        epoch_id=epoch_id,
        trade_count=trade_count,
        r_distribution=r_dist,
        expectancy_ci=exp_ci,
        optimal_f_ci=opt_ci,
        sufficient_for_ci=sufficient_ci,
        sufficient_for_step_up=sufficient_step,
        trades_needed_for_ci=trades_for_ci,
        trades_needed_for_step_up=trades_for_step,
    )


def _format_statistics(edge: EdgeEstimate) -> str:
    lines = [f"Segment: {edge.silo.value} / {edge.epoch_id} — {edge.trade_count} closed trades"]
    if edge.r_distribution:
        rd = edge.r_distribution
        lines.append(
            f"Expectancy {rd.expectancy:.2f}R · Win rate {rd.win_rate:.0%} · "
            f"Mean {rd.mean_r:.2f}R · Std {rd.std_r:.2f} · Skew {rd.skew:.2f}"
        )
    if edge.expectancy_ci:
        ec = edge.expectancy_ci
        lines.append(
            f"Expectancy CI ({100 - ec.ci_level * 100:.0f}%): "
            f"{ec.lower:.2f}R to {ec.upper:.2f}R (point {ec.point:.2f}R)"
        )
    if edge.optimal_f_ci:
        oc = edge.optimal_f_ci
        lines.append(
            f"Optimal-f CI ({100 - oc.ci_level * 100:.0f}%): "
            f"{oc.lower:.3f} to {oc.upper:.3f} (point {oc.point:.3f})"
        )
    return "\n".join(lines)


def recommend_risk_appetite(
    events: list[TradeEvent],
    silo: Silo,
    *,
    config: AdaptiveRiskConfig | None = None,
    sizing: SizingConfig | None = None,
    drawdown_pct: float = 0.0,
    epoch_id: str | None = None,
) -> RiskAppetiteRecommendation:
    """Produce risk-appetite recommendation for one silo (FR-4.2–4.6)."""
    cfg = config or DEFAULT_ADAPTIVE_RISK_CONFIG
    live_sizing = sizing or DEFAULT_SIZING_CONFIG
    post_epoch = epoch_id or (DEFAULT_EPOCHS[-1].epoch_id if DEFAULT_EPOCHS else None)
    if not post_epoch:
        post_epoch = "unknown"

    entries = extract_closed_entries(events)
    seg = [
        e
        for e in entries
        if e.silo == silo and e.epoch_id == post_epoch and e.realized_r is not None
    ]
    r_vals = [e.realized_r for e in seg if e.realized_r is not None]
    edge = compute_edge_estimate(r_vals, silo=silo, epoch_id=post_epoch, config=cfg)

    dd_mult, dd_msg = _drawdown_multiplier(drawdown_pct, cfg)
    warnings: list[str] = []
    if dd_msg:
        warnings.append(dd_msg)

    current_base_f = live_sizing.base_risk_f
    current_kelly = live_sizing.kelly_fraction
    recommended_base_f = current_base_f
    recommended_kelly = current_kelly
    action = RiskAction.HOLD
    withhold_reason: str | None = None

    lower_f = edge.optimal_f_ci.lower if edge.optimal_f_ci else None
    lower_exp = edge.expectancy_ci.lower if edge.expectancy_ci else None

    # Derive conservative live f from lower CI (FR-4.2)
    if lower_f is not None and lower_f > 0:
        effective_f = lower_f * current_kelly * dd_mult
    else:
        effective_f = current_base_f * dd_mult

    if edge.trade_count < cfg.min_trades_for_ci:
        action = RiskAction.INSUFFICIENT_DATA
        withhold_reason = (
            f"Only {edge.trade_count} closed trades in {post_epoch}; "
            f"need ≈{edge.trades_needed_for_ci} more for reliable edge estimates."
        )
        narrative = (
            f"**Hold current settings** ({current_base_f:.2%} base f, {_kelly_label(current_kelly)}). "
            f"{withhold_reason} "
            f"Conservative sizing continues at {current_base_f:.2%} until the post-change sample grows."
        )
    elif dd_mult <= 0:
        action = RiskAction.DE_RISK
        recommended_base_f = 0.0
        recommended_kelly = current_kelly
        withhold_reason = dd_msg
        narrative = (
            f"**De-risk — no step-ups.** Account drawdown is at the hard tolerance ({drawdown_pct:.1%}). "
            f"Hold {_kelly_label(current_kelly)} but suspend new risk until drawdown recovers."
        )
    elif not edge.sufficient_for_step_up:
        action = RiskAction.HOLD
        withhold_reason = (
            f"Post-change sample has {edge.trade_count} trades; "
            f"≈{edge.trades_needed_for_step_up} more closed trades needed before a Kelly step-up."
        )
        narrative = (
            f"**Hold {_kelly_label(current_kelly)}** at {current_base_f:.2%} base f. "
            f"Edge estimates are forming but sample size is still thin. {withhold_reason}"
        )
        if lower_f is not None:
            narrative += (
                f" Lower-CI optimal-f is {lower_f:.3f} "
                f"(threshold {cfg.step_up_optimal_f_lower_threshold:.3f} for step-up)."
            )
    else:
        # Step-up gating on lower CI (FR-4.3)
        can_step_kelly = (
            lower_f is not None
            and lower_f >= cfg.step_up_optimal_f_lower_threshold
            and (lower_exp is None or lower_exp >= cfg.step_up_expectancy_lower_threshold)
        )
        next_kelly = _next_ladder_step(current_kelly, cfg.kelly_ladder)
        next_base_f = _next_ladder_step(current_base_f, cfg.base_f_ladder)

        if can_step_kelly and next_kelly is not None:
            action = RiskAction.STEP_UP_KELLY
            recommended_kelly = next_kelly
            effective_f = lower_f * recommended_kelly * dd_mult
            narrative = (
                f"**Step up to {_kelly_label(recommended_kelly)}.** "
                f"Post-change lower-CI optimal-f ({lower_f:.3f}) clears the "
                f"{cfg.step_up_optimal_f_lower_threshold:.3f} threshold with "
                f"{edge.trade_count} trades. "
                f"Conservative live f ≈ {effective_f:.2%} of equity "
                f"(lower-CI optimal-f × {_kelly_label(recommended_kelly)}"
                f"{f' × {dd_mult:.0%} drawdown cap' if dd_mult < 1 else ''})."
            )
            if lower_exp is not None:
                narrative += f" Expectancy lower bound: {lower_exp:.2f}R."
        elif can_step_kelly and next_base_f is not None and lower_f >= next_base_f:
            action = RiskAction.STEP_UP_BASE_F
            recommended_base_f = next_base_f
            effective_f = min(lower_f * current_kelly, recommended_base_f) * dd_mult
            narrative = (
                f"**Raise base f to {recommended_base_f:.2%}.** "
                f"Edge lower bound supports a higher baseline risk fraction while staying at "
                f"{_kelly_label(current_kelly)}. Effective sizing f ≈ {effective_f:.2%}."
            )
        else:
            action = RiskAction.HOLD
            if lower_f is not None and lower_f < cfg.step_up_optimal_f_lower_threshold:
                withhold_reason = (
                    f"Lower-CI optimal-f ({lower_f:.3f}) has not cleared the "
                    f"{cfg.step_up_optimal_f_lower_threshold:.3f} step-up threshold."
                )
                trades_needed = _estimate_trades_needed(
                    edge.trade_count,
                    cfg.min_trades_for_step_up,
                    lower_f,
                    cfg.step_up_optimal_f_lower_threshold,
                )
                narrative = (
                    f"**Hold {_kelly_label(current_kelly)}** at {current_base_f:.2%} base f. "
                    f"{withhold_reason} "
                    f"At current dispersion, ≈{trades_needed} more closed trades before I'd recommend "
                    f"{_kelly_label(next_kelly) if next_kelly else 'a higher Kelly step'}."
                )
            else:
                narrative = (
                    f"**Hold {_kelly_label(current_kelly)}** at {current_base_f:.2%} base f. "
                    f"Statistics support current risk; no step-up warranted yet."
                )

    if dd_mult < 1.0 and action in (RiskAction.STEP_UP_KELLY, RiskAction.STEP_UP_BASE_F):
        action = RiskAction.HOLD
        recommended_kelly = current_kelly
        recommended_base_f = current_base_f
        effective_f = (lower_f or current_base_f) * current_kelly * dd_mult
        withhold_reason = dd_msg
        narrative = (
            f"**Hold — drawdown overrides edge.** {dd_msg} "
            f"Step-up withheld despite favorable edge statistics. "
            f"Effective f capped at {effective_f:.2%}."
        )

    return RiskAppetiteRecommendation(
        silo=silo,
        epoch_id=post_epoch,
        action=action,
        current_base_f=current_base_f,
        recommended_base_f=recommended_base_f * dd_mult if recommended_base_f > 0 else 0.0,
        current_kelly_fraction=current_kelly,
        recommended_kelly_fraction=recommended_kelly,
        effective_risk_f=effective_f,
        drawdown_multiplier=dd_mult,
        narrative=narrative,
        statistics=_format_statistics(edge),
        edge=edge,
        withhold_reason=withhold_reason,
        warnings=warnings,
    )


def build_risk_review(
    events: list[TradeEvent],
    *,
    config: AdaptiveRiskConfig | None = None,
    sizing: SizingConfig | None = None,
    drawdown_pct: float = 0.0,
) -> RiskReviewReport:
    """Full edge & risk review for both silos and all epoch segments."""
    cfg = config or DEFAULT_ADAPTIVE_RISK_CONFIG
    live_sizing = sizing or DEFAULT_SIZING_CONFIG
    post_epoch = DEFAULT_EPOCHS[-1].epoch_id if DEFAULT_EPOCHS else None
    entries = extract_closed_entries(events)
    epoch_ids = sorted({e.epoch_id for e in entries if e.epoch_id})

    segments: list[EdgeEstimate] = []
    for silo in (Silo.STOCK_OPTIONS, Silo.FUTURES):
        for epoch_id in epoch_ids:
            seg = [
                e
                for e in entries
                if e.silo == silo and e.epoch_id == epoch_id and e.realized_r is not None
            ]
            if not seg:
                continue
            r_vals = [e.realized_r for e in seg if e.realized_r is not None]
            segments.append(compute_edge_estimate(r_vals, silo=silo, epoch_id=epoch_id, config=cfg))

    recommendations = [
        recommend_risk_appetite(
            events,
            silo,
            config=cfg,
            sizing=live_sizing,
            drawdown_pct=drawdown_pct,
            epoch_id=post_epoch,
        )
        for silo in (Silo.STOCK_OPTIONS, Silo.FUTURES)
    ]

    return RiskReviewReport(
        recommendations=recommendations,
        epoch_segments=segments,
        post_epoch_id=post_epoch,
    )


def format_risk_recommendation(rec: RiskAppetiteRecommendation) -> str:
    """Plain-text output for CLI."""
    lines = [
        f"Risk Appetite — {rec.silo.value} ({rec.epoch_id})",
        "=" * 48,
        rec.narrative.replace("**", ""),
        "",
        f"Action: {rec.action.value}",
        f"Base f: {rec.current_base_f:.2%} → recommended {rec.recommended_base_f:.2%}",
        f"Kelly: {_kelly_label(rec.current_kelly_fraction)} → {_kelly_label(rec.recommended_kelly_fraction)}",
        f"Effective risk f (lower-CI): {rec.effective_risk_f:.3%}",
        f"Drawdown multiplier: {rec.drawdown_multiplier:.0%}",
        "",
        rec.statistics,
    ]
    if rec.withhold_reason:
        lines.extend(["", f"Withheld: {rec.withhold_reason}"])
    if rec.warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in rec.warnings:
            lines.append(f"  • {w}")
    return "\n".join(lines)


def format_risk_review(report: RiskReviewReport) -> str:
    """Plain-text summary for CLI."""
    lines = ["Edge & Risk Review", "=" * 48]
    if report.post_epoch_id:
        lines.append(f"Post-change epoch: {report.post_epoch_id}")
    lines.append("")

    for rec in report.recommendations:
        lines.append(format_risk_recommendation(rec))
        lines.append("")

    if report.epoch_segments:
        lines.append("--- All epoch segments ---")
        for seg in report.epoch_segments:
            rd = seg.r_distribution
            exp = f"{rd.expectancy:.2f}R" if rd else "n/a"
            opt = f"{seg.optimal_f_ci.point:.3f}" if seg.optimal_f_ci else "n/a"
            lower = f"{seg.optimal_f_ci.lower:.3f}" if seg.optimal_f_ci else "n/a"
            lines.append(
                f"  {seg.silo.value} / {seg.epoch_id}: {seg.trade_count} trades · "
                f"E={exp} · optimal-f={opt} (lower CI {lower})"
            )

    return "\n".join(lines)
