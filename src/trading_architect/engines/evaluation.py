"""Evaluation engine — alpha left on the table (PRD §7.6, M2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from trading_architect.assembly.entries import ClosedTradeEntry, extract_closed_entries
from trading_architect.config.defaults import (
    DEFAULT_BASE_RISK_F,
    DEFAULT_KELLY_FRACTION,
    DEFAULT_LEVERAGE_CAP,
)
from trading_architect.engines.equity import build_equity_lookup
from trading_architect.engines.formulas import fractional_risk_size
from trading_architect.engines.r_distribution import RDistributionStats, compute_r_distribution
from trading_architect.models.entities import Silo, TradeEvent


class CounterfactualRule(str, Enum):
    ACTUAL = "actual"
    FRACTIONAL_1PCT = "fractional_1pct"
    FRACTIONAL_2PCT = "fractional_2pct"
    KELLY_QUARTER = "kelly_quarter"
    KELLY_THIRD = "kelly_third"
    KELLY_HALF = "kelly_half"


@dataclass
class TradeContribution:
    entry_id: str
    underlying: str
    symbol: str
    asset_type: str
    silo: str
    epoch_id: str | None
    opened_at: str
    closed_at: str
    actual_pnl: float
    counterfactual_pnl: float
    alpha_left: float
    actual_qty: float
    counterfactual_qty: float
    realized_r: float | None
    initial_risk: float
    risk_basis: str


@dataclass
class OptionsWinnerAnalysis:
    """FR-6.3 — isolate under-sizing on rare large winners in the options book."""

    big_winner_threshold_r: float
    big_winner_count: int
    alpha_from_big_winners: float
    alpha_from_other_trades: float
    pct_alpha_from_big_winners: float
    narrative: str


@dataclass
class SegmentEvaluation:
    silo: Silo
    epoch_id: str
    rule: CounterfactualRule
    trade_count: int
    total_actual_pnl: float
    total_counterfactual_pnl: float
    alpha_left_on_table: float
    r_distribution: RDistributionStats | None
    kelly_f_source: str | None = None
    contributions: list[TradeContribution] = field(default_factory=list)
    options_winner_analysis: OptionsWinnerAnalysis | None = None


@dataclass
class EvaluationReport:
    segments: list[SegmentEvaluation]
    starting_equity_by_silo: dict[str, float]
    rules_evaluated: list[str]

    def total_alpha(self, silo: Silo | None = None, rule: CounterfactualRule | None = None) -> float:
        total = 0.0
        for seg in self.segments:
            if silo and seg.silo != silo:
                continue
            if rule and seg.rule != rule:
                continue
            total += seg.alpha_left_on_table
        return total


def _target_risk_budget(
    rule: CounterfactualRule,
    equity: float,
    optimal_f: float,
) -> float:
    """Dollar risk budget per trade under a counterfactual rule."""
    if rule == CounterfactualRule.FRACTIONAL_1PCT:
        return DEFAULT_BASE_RISK_F * equity
    if rule == CounterfactualRule.FRACTIONAL_2PCT:
        return 0.02 * equity
    if rule == CounterfactualRule.KELLY_QUARTER:
        return optimal_f * 0.25 * equity
    if rule == CounterfactualRule.KELLY_THIRD:
        return optimal_f * (1 / 3) * equity
    if rule == CounterfactualRule.KELLY_HALF:
        return optimal_f * 0.5 * equity
    return equity * DEFAULT_BASE_RISK_F


def _is_kelly_rule(rule: CounterfactualRule) -> bool:
    return rule in (
        CounterfactualRule.KELLY_QUARTER,
        CounterfactualRule.KELLY_THIRD,
        CounterfactualRule.KELLY_HALF,
    )


def _prior_epoch_optimal_f(
    entries: list[ClosedTradeEntry],
    silo: Silo,
    epoch_id: str,
    epoch_ids: list[str],
) -> tuple[float | None, str]:
    """Out-of-sample optimal-f from the immediately prior epoch segment."""
    if epoch_id not in epoch_ids:
        return None, "no_prior_epoch"
    idx = epoch_ids.index(epoch_id)
    if idx == 0:
        return None, "first_epoch_no_prior"
    prior_epoch = epoch_ids[idx - 1]
    prior_entries = [
        e for e in entries if e.silo == silo and e.epoch_id == prior_epoch and e.realized_r is not None
    ]
    prior_r = [e.realized_r for e in prior_entries if e.realized_r is not None]
    prior_dist = compute_r_distribution(prior_r)
    if not prior_dist or prior_dist.optimal_f <= 0:
        return None, f"prior_epoch_{prior_epoch}_insufficient"
    return prior_dist.optimal_f, f"prior_epoch:{prior_epoch}"


def _counterfactual_qty(
    entry: ClosedTradeEntry,
    rule: CounterfactualRule,
    equity_at_entry: float,
    optimal_f: float,
) -> float:
    if rule == CounterfactualRule.ACTUAL:
        return entry.quantity

    if entry.risk_per_unit <= 0:
        return entry.quantity

    budget = _target_risk_budget(rule, equity_at_entry, optimal_f)
    target_qty = fractional_risk_size(equity_at_entry, budget / equity_at_entry, entry.risk_per_unit)

    # Leverage cap proxy: notional cannot exceed leverage_cap × equity (stock/options)
    if entry.silo == Silo.STOCK_OPTIONS and entry.asset_type.value != "option":
        notional_per_unit = entry.entry_price
        max_qty = (DEFAULT_LEVERAGE_CAP * equity_at_entry) / notional_per_unit
        target_qty = min(target_qty, max_qty)
    elif entry.asset_type.value == "option":
        spot = entry.spot_at_entry or entry.entry_price
        delta = entry.option_delta if entry.option_delta is not None else 0.5
        notional_per_unit = abs(delta) * 100 * spot
        max_qty = (DEFAULT_LEVERAGE_CAP * equity_at_entry) / max(notional_per_unit, 1.0)
        target_qty = min(target_qty, max_qty)

    return max(target_qty, 0.0)


def _scale_pnl(actual_pnl: float, actual_qty: float, counterfactual_qty: float) -> float:
    if actual_qty <= 0:
        return actual_pnl
    return actual_pnl * (counterfactual_qty / actual_qty)


def _options_winner_analysis(
    contributions: list[TradeContribution],
    threshold_r: float = 3.0,
) -> OptionsWinnerAnalysis | None:
    option_contribs = [c for c in contributions if c.asset_type == "option"]
    if not option_contribs:
        return None

    big = [c for c in option_contribs if c.realized_r is not None and c.realized_r >= threshold_r]
    if not big:
        return OptionsWinnerAnalysis(
            big_winner_threshold_r=threshold_r,
            big_winner_count=0,
            alpha_from_big_winners=0.0,
            alpha_from_other_trades=sum(c.alpha_left for c in option_contribs),
            pct_alpha_from_big_winners=0.0,
            narrative="No options trades exceeded the big-winner R threshold in this segment.",
        )

    alpha_big = sum(c.alpha_left for c in big)
    alpha_other = sum(c.alpha_left for c in option_contribs if c not in big)
    total_alpha = alpha_big + alpha_other
    pct = (alpha_big / total_alpha * 100) if total_alpha > 0 else 0.0

    return OptionsWinnerAnalysis(
        big_winner_threshold_r=threshold_r,
        big_winner_count=len(big),
        alpha_from_big_winners=alpha_big,
        alpha_from_other_trades=alpha_other,
        pct_alpha_from_big_winners=pct,
        narrative=(
            f"{pct:.0f}% of options-book alpha left ({alpha_big:,.0f} USD) came from "
            f"under-sizing {len(big)} rare large winners (≥{threshold_r}R). "
            f"Remaining {alpha_other:,.0f} USD from other option trades."
        ),
    )


def evaluate_alpha_left(
    events: list[TradeEvent],
    *,
    starting_equity: dict[Silo, float] | None = None,
    rules: list[CounterfactualRule] | None = None,
    big_winner_r_threshold: float = 3.0,
) -> EvaluationReport:
    """Run full evaluation: R-distributions + counterfactual alpha per silo/epoch."""
    default_start = {
        Silo.STOCK_OPTIONS: 100_000.0,
        Silo.FUTURES: 50_000.0,
    }
    starting = starting_equity or default_start

    entries = extract_closed_entries(events)
    equity_lookup = build_equity_lookup(events, entries, starting)

    if rules is None:
        rules = [
            CounterfactualRule.FRACTIONAL_1PCT,
            CounterfactualRule.FRACTIONAL_2PCT,
            CounterfactualRule.KELLY_QUARTER,
            CounterfactualRule.KELLY_THIRD,
            CounterfactualRule.KELLY_HALF,
        ]

    segments: list[SegmentEvaluation] = []
    epoch_ids = sorted({e.epoch_id for e in entries if e.epoch_id})

    for silo in (Silo.STOCK_OPTIONS, Silo.FUTURES):
        for epoch_id in epoch_ids:
            seg_entries = [
                e for e in entries if e.silo == silo and e.epoch_id == epoch_id
            ]
            if not seg_entries:
                continue

            r_vals = [e.realized_r for e in seg_entries if e.realized_r is not None]
            r_dist = compute_r_distribution(r_vals)
            kelly_f, kelly_source = _prior_epoch_optimal_f(entries, silo, epoch_id, epoch_ids)

            for rule in rules:
                if _is_kelly_rule(rule) and kelly_f is None:
                    segments.append(
                        SegmentEvaluation(
                            silo=silo,
                            epoch_id=epoch_id,
                            rule=rule,
                            trade_count=len(seg_entries),
                            total_actual_pnl=sum(e.realized_pnl for e in seg_entries),
                            total_counterfactual_pnl=sum(e.realized_pnl for e in seg_entries),
                            alpha_left_on_table=0.0,
                            r_distribution=r_dist,
                            kelly_f_source=kelly_source,
                            contributions=[],
                        )
                    )
                    continue

                rule_kelly_f = kelly_f if _is_kelly_rule(rule) else (r_dist.optimal_f if r_dist else DEFAULT_KELLY_FRACTION)
                rule_kelly_source = kelly_source if _is_kelly_rule(rule) else None

                contributions: list[TradeContribution] = []
                total_actual = 0.0
                total_cf = 0.0

                for entry in seg_entries:
                    eq = equity_lookup(silo, entry.opened_at.date())
                    cf_qty = _counterfactual_qty(entry, rule, eq, rule_kelly_f)
                    cf_pnl = _scale_pnl(entry.realized_pnl, entry.quantity, cf_qty)
                    alpha = cf_pnl - entry.realized_pnl

                    contributions.append(
                        TradeContribution(
                            entry_id=entry.entry_id,
                            underlying=entry.underlying,
                            symbol=entry.symbol,
                            asset_type=entry.asset_type.value,
                            silo=silo.value,
                            epoch_id=entry.epoch_id,
                            opened_at=entry.opened_at.isoformat(),
                            closed_at=entry.closed_at.isoformat(),
                            actual_pnl=entry.realized_pnl,
                            counterfactual_pnl=cf_pnl,
                            alpha_left=alpha,
                            actual_qty=entry.quantity,
                            counterfactual_qty=cf_qty,
                            realized_r=entry.realized_r,
                            initial_risk=entry.initial_risk,
                            risk_basis=entry.risk_basis,
                        )
                    )
                    total_actual += entry.realized_pnl
                    total_cf += cf_pnl

                options_analysis = None
                if silo == Silo.STOCK_OPTIONS:
                    options_analysis = _options_winner_analysis(
                        contributions, threshold_r=big_winner_r_threshold
                    )

                segments.append(
                    SegmentEvaluation(
                        silo=silo,
                        epoch_id=epoch_id,
                        rule=rule,
                        trade_count=len(seg_entries),
                        total_actual_pnl=total_actual,
                        total_counterfactual_pnl=total_cf,
                        alpha_left_on_table=total_cf - total_actual,
                        r_distribution=r_dist,
                        kelly_f_source=rule_kelly_source,
                        contributions=contributions,
                        options_winner_analysis=options_analysis,
                    )
                )

    return EvaluationReport(
        segments=segments,
        starting_equity_by_silo={k.value: v for k, v in starting.items()},
        rules_evaluated=[r.value for r in rules],
    )


def format_report_summary(report: EvaluationReport) -> str:
    """Plain-text summary for CLI output."""
    lines = ["Alpha Left on the Table — Evaluation Summary", "=" * 48]
    lines.append(
        f"Starting equity (stock/options): ${report.starting_equity_by_silo.get('stock_options', 0):,.0f}"
    )
    lines.append(
        f"Starting equity (futures): ${report.starting_equity_by_silo.get('futures', 0):,.0f}"
    )
    lines.append("")

    seen: set[tuple[str, str]] = set()
    for seg in report.segments:
        key = (seg.silo.value, seg.epoch_id)
        if key not in seen:
            seen.add(key)
            if seg.r_distribution:
                rd = seg.r_distribution
                lines.append(f"--- {seg.silo.value} / {seg.epoch_id} — R-distribution ---")
                lines.append(f"  Trades: {rd.count} | Expectancy: {rd.expectancy:.2f}R | Win rate: {rd.win_rate:.0%}")
                lines.append(
                    f"  Mean: {rd.mean_r:.2f}R | Std: {rd.std_r:.2f} | Skew: {rd.skew:.2f} | "
                    f"P5/P95: {rd.p05_r:.2f}/{rd.p95_r:.2f}R"
                )
                lines.append(f"  Optimal-f: {rd.optimal_f:.3f} | ¼-Kelly f: {rd.kelly_quarter_f:.3f}")
                lines.append("")

    lines.append("--- Counterfactual alpha (USD) ---")
    for seg in report.segments:
        kelly_note = ""
        if seg.kelly_f_source and _is_kelly_rule(seg.rule):
            kelly_note = f" [Kelly-f: {seg.kelly_f_source}]"
        lines.append(
            f"  {seg.silo.value} / {seg.epoch_id} / {seg.rule.value}{kelly_note}: "
            f"${seg.alpha_left_on_table:,.0f} "
            f"(actual ${seg.total_actual_pnl:,.0f} → CF ${seg.total_counterfactual_pnl:,.0f}, "
            f"{seg.trade_count} trades)"
        )
        if seg.options_winner_analysis and seg.rule == CounterfactualRule.FRACTIONAL_1PCT:
            lines.append(f"    → {seg.options_winner_analysis.narrative}")

    return "\n".join(lines)
