"""Six-layer sizing engine — PRD §7.3 (M3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from trading_architect.assembly.entries import extract_closed_entries
from trading_architect.assembly.positions import futures_multiplier
from trading_architect.config.defaults import DEFAULT_EPOCHS, OPTION_CONTRACT_MULTIPLIER
from trading_architect.config.sizing import DEFAULT_SIZING_CONFIG, EquityTier, SizingConfig
from trading_architect.engines.drawdown import attribution_aware_throttle
from trading_architect.engines.formulas import (
    delta_adjusted_notional,
    dollar_risk_stock,
    fractional_risk_size,
    leverage_ratio,
    portfolio_heat,
)
from trading_architect.engines.r_distribution import (
    bootstrap_optimal_f_lower,
    compute_r_distribution,
)
from trading_architect.models.entities import AssetType, Direction, Position, Silo, TradeEvent


class BindingConstraint(str, Enum):
    FRACTIONAL_RISK = "fractional_risk"
    VOLATILITY = "volatility"
    KELLY = "kelly"
    EQUITY_TIER = "equity_tier"
    HEAT_CAP = "heat_cap"
    LEVERAGE_CAP = "leverage_cap"
    DRAWDOWN_THROTTLE = "drawdown_throttle"
    INVALID_INPUT = "invalid_input"


@dataclass
class CandidateTrade:
    """Minimal inputs for sizing a new entry (FR-3.1)."""

    silo: Silo
    underlying: str
    direction: Direction
    asset_type: AssetType
    entry_price: float
    stop_price: float | None = None
    premium_per_contract: float | None = None
    spot_price: float | None = None
    option_delta: float | None = None
    symbol: str | None = None
    atr: float | None = None


@dataclass
class SiloExposure:
    """Current open-book exposure for ceiling checks (FR-3.6)."""

    silo_equity: float
    open_dollar_risk: float = 0.0
    open_delta_notional: float = 0.0
    drawdown_pct: float = 0.0
    peak_equity: float | None = None


@dataclass
class LayerResult:
    layer: int
    name: str
    recommended_qty: float
    effective_f: float | None = None
    detail: str = ""


@dataclass
class SizeRecommendation:
    recommended_qty: float
    recommended_qty_int: int
    dollar_risk: float
    delta_notional: float
    binding_constraint: BindingConstraint
    rationale: str
    layers: list[LayerResult] = field(default_factory=list)
    heat_after: float = 0.0
    leverage_after: float = 0.0
    kelly_target_f: float | None = None
    warnings: list[str] = field(default_factory=list)


def _effective_f_for_equity(
    equity: float, base_f: float, tiers: list[EquityTier]
) -> tuple[float, str]:
    """Layer 5 — band base f by equity tier."""
    applicable = [t for t in sorted(tiers, key=lambda t: t.min_equity) if equity >= t.min_equity]
    if not applicable:
        return base_f, "base tier"
    tier = applicable[-1]
    return base_f * tier.f_multiplier, tier.label or f"≥${tier.min_equity:,.0f}"


def _risk_per_unit(candidate: CandidateTrade) -> tuple[float, str]:
    symbol = candidate.symbol or candidate.underlying
    if candidate.asset_type == AssetType.OPTION:
        premium = candidate.premium_per_contract or candidate.entry_price
        rpu = premium * OPTION_CONTRACT_MULTIPLIER
        return rpu, "premium-at-risk (max loss = premium paid)"

    if candidate.stop_price is None:
        return 0.0, "stop price required for stock/futures"

    mult = futures_multiplier(symbol) if candidate.asset_type == AssetType.FUTURE else 1.0
    rpu = dollar_risk_stock(candidate.entry_price, candidate.stop_price, 1.0, mult)
    return rpu, f"price stop (${candidate.stop_price:.2f})"


def _notional_per_unit(candidate: CandidateTrade) -> float:
    spot = candidate.spot_price or candidate.entry_price
    symbol = candidate.symbol or candidate.underlying

    if candidate.asset_type == AssetType.OPTION:
        delta = candidate.option_delta if candidate.option_delta is not None else 0.5
        return abs(delta) * OPTION_CONTRACT_MULTIPLIER * spot

    if candidate.asset_type == AssetType.FUTURE:
        return candidate.entry_price * futures_multiplier(symbol)

    return spot


def _optimal_f_for_silo(
    events: list[TradeEvent],
    silo: Silo,
    config: SizingConfig,
) -> tuple[float | None, float | None]:
    """Point optimal-f and optional lower-CI bound from post-change epoch when possible."""
    entries = extract_closed_entries(events)
    post_epoch = DEFAULT_EPOCHS[-1].epoch_id if DEFAULT_EPOCHS else None
    seg = [
        e
        for e in entries
        if e.silo == silo
        and e.realized_r is not None
        and (post_epoch is None or e.epoch_id == post_epoch)
    ]
    if len(seg) < 3:
        seg = [e for e in entries if e.silo == silo and e.realized_r is not None]

    r_vals = [e.realized_r for e in seg if e.realized_r is not None]
    if not r_vals:
        return None, None

    dist = compute_r_distribution(r_vals)
    if not dist:
        return None, None

    lower = None
    if config.use_kelly_lower_ci:
        lower = bootstrap_optimal_f_lower(r_vals, n_bootstrap=config.bootstrap_samples)

    return dist.optimal_f, lower


def aggregate_open_exposure(
    positions: list[Position],
    silo: Silo,
    silo_equity: float,
) -> SiloExposure:
    """Sum open position risk and notional for a silo."""
    open_positions = [p for p in positions if p.silo == silo and p.status.value == "open"]
    return SiloExposure(
        silo_equity=silo_equity,
        open_dollar_risk=sum(p.total_dollar_risk for p in open_positions),
        open_delta_notional=sum(p.current_delta_notional for p in open_positions),
    )


def recommend_size(
    candidate: CandidateTrade,
    exposure: SiloExposure,
    *,
    config: SizingConfig | None = None,
    events: list[TradeEvent] | None = None,
    optimal_f_override: float | None = None,
    drawdown_correlation: float = 1.0,
) -> SizeRecommendation:
    """Compose six sizing layers and return a capped recommendation with rationale.

    ``drawdown_correlation`` is the candidate opportunity's correlation to the
    cluster currently driving the drawdown (see
    ``drawdown.candidate_drawdown_correlation``). It defaults to 1.0 — the
    conservative case, identical to a uniform governor — so callers that don't
    supply attribution get the safe behavior. A measurably uncorrelated candidate
    (value near 0) is sized at full edge even while the book is underwater; the
    20% hard cap still suspends all new risk regardless of correlation.
    """
    cfg = config or DEFAULT_SIZING_CONFIG
    layers: list[LayerResult] = []
    warnings: list[str] = []

    rpu, risk_basis = _risk_per_unit(candidate)
    if rpu <= 0:
        return SizeRecommendation(
            recommended_qty=0.0,
            recommended_qty_int=0,
            dollar_risk=0.0,
            delta_notional=0.0,
            binding_constraint=BindingConstraint.INVALID_INPUT,
            rationale=f"Cannot size: invalid risk basis ({risk_basis}).",
            layers=layers,
            warnings=["Provide a stop for stock/futures or premium for options."],
        )

    corr_to_source = drawdown_correlation if cfg.attribution_aware_governor else 1.0
    throttle = attribution_aware_throttle(exposure.drawdown_pct, corr_to_source, cfg)
    throttle_mult, throttle_msg = throttle.throttle_multiplier, throttle.message
    if throttle_mult <= 0:
        return SizeRecommendation(
            recommended_qty=0.0,
            recommended_qty_int=0,
            dollar_risk=0.0,
            delta_notional=0.0,
            binding_constraint=BindingConstraint.DRAWDOWN_THROTTLE,
            rationale=throttle_msg or "Drawdown governor suspended new risk.",
            layers=layers,
            warnings=[throttle_msg] if throttle_msg else [],
        )
    if throttle_msg:
        warnings.append(throttle_msg)

    tier_f, tier_label = _effective_f_for_equity(
        exposure.silo_equity, cfg.base_risk_f, cfg.equity_tiers
    )
    effective_f = tier_f * throttle_mult

    # Layer 2 — fractional risk
    qty_l2 = fractional_risk_size(exposure.silo_equity, effective_f, rpu)
    layers.append(
        LayerResult(
            layer=2,
            name="Fractional risk",
            recommended_qty=qty_l2,
            effective_f=effective_f,
            detail=f"{effective_f:.2%} × ${exposure.silo_equity:,.0f} equity / ${rpu:,.2f} risk per unit ({tier_label})",
        )
    )

    # Layer 3 — volatility normalization
    qty_l3 = qty_l2
    if candidate.atr and candidate.atr > 0 and cfg.reference_atr and cfg.reference_atr > 0:
        vol_factor = cfg.reference_atr / candidate.atr
        qty_l3 = qty_l2 * vol_factor
        layers.append(
            LayerResult(
                layer=3,
                name="Volatility normalization",
                recommended_qty=qty_l3,
                detail=f"ATR scale {vol_factor:.2f}× (ref {cfg.reference_atr:.2f} / instrument {candidate.atr:.2f})",
            )
        )
    else:
        layers.append(
            LayerResult(
                layer=3,
                name="Volatility normalization",
                recommended_qty=qty_l3,
                detail="Skipped — no ATR or reference ATR configured",
            )
        )

    # Layer 4 — fractional Kelly (lower CI when available)
    optimal_f, optimal_f_lower = None, None
    if optimal_f_override is not None:
        optimal_f = optimal_f_override
    elif events:
        optimal_f, optimal_f_lower = _optimal_f_for_silo(events, candidate.silo, cfg)

    kelly_f_used = None
    qty_l4 = qty_l3
    if optimal_f is not None:
        base_kelly_f = (
            optimal_f_lower
            if (cfg.use_kelly_lower_ci and optimal_f_lower is not None)
            else optimal_f
        )
        kelly_f_used = base_kelly_f * cfg.kelly_fraction * throttle_mult
        qty_l4 = fractional_risk_size(exposure.silo_equity, kelly_f_used, rpu)
        ci_note = "lower-CI optimal-f" if optimal_f_lower is not None else "point optimal-f"
        layers.append(
            LayerResult(
                layer=4,
                name="Fractional Kelly",
                recommended_qty=qty_l4,
                effective_f=kelly_f_used,
                detail=f"{cfg.kelly_fraction:.0%} Kelly on {ci_note} ({base_kelly_f:.3f} → {kelly_f_used:.3%} of equity)",
            )
        )
    else:
        layers.append(
            LayerResult(
                layer=4,
                name="Fractional Kelly",
                recommended_qty=qty_l4,
                detail="Skipped — insufficient closed-trade history for optimal-f",
            )
        )
        warnings.append("Kelly layer inactive until more closed trades exist in this silo.")

    # Layer 5 — equity scaling (already in effective_f via tiers)
    layers.append(
        LayerResult(
            layer=5,
            name="Equity scaling",
            recommended_qty=qty_l3,
            effective_f=tier_f,
            detail=f"Tier '{tier_label}' → base f {cfg.base_risk_f:.2%} × {tier_f / cfg.base_risk_f:.1f}",
        )
    )

    qty_pre_cap = min(qty_l2, qty_l3, qty_l4)

    # Layer 6 — heat & leverage ceilings
    max_qty_heat = float("inf")
    max_qty_lev = float("inf")
    notional_per = _notional_per_unit(candidate)

    remaining_heat_budget = cfg.heat_cap * exposure.silo_equity - exposure.open_dollar_risk
    if remaining_heat_budget <= 0:
        max_qty_heat = 0.0
    else:
        max_qty_heat = remaining_heat_budget / rpu

    if notional_per > 0:
        remaining_notional = cfg.leverage_cap * exposure.silo_equity - exposure.open_delta_notional
        max_qty_lev = max(0.0, remaining_notional / notional_per) if remaining_notional > 0 else 0.0

    qty_final = min(qty_pre_cap, max_qty_heat, max_qty_lev)
    qty_final = max(qty_final, 0.0)

    dollar_risk = qty_final * rpu
    spot = candidate.spot_price or candidate.entry_price
    if candidate.asset_type == AssetType.OPTION:
        delta = candidate.option_delta if candidate.option_delta is not None else 0.5
        delta_notional = delta_adjusted_notional(0, spot, [(qty_final, delta)])
    elif candidate.asset_type == AssetType.STOCK:
        delta_notional = qty_final * spot
    else:
        delta_notional = qty_final * notional_per

    heat_after = portfolio_heat(exposure.open_dollar_risk + dollar_risk, exposure.silo_equity)
    lev_after = leverage_ratio(exposure.open_delta_notional + delta_notional, exposure.silo_equity)

    layers.append(
        LayerResult(
            layer=6,
            name="Heat & leverage ceilings",
            recommended_qty=qty_final,
            detail=(
                f"Heat cap {cfg.heat_cap:.0%} → max {max_qty_heat:.1f} units; "
                f"Leverage cap {cfg.leverage_cap:.1f}× → max {max_qty_lev:.1f} units"
            ),
        )
    )

    binding = BindingConstraint.FRACTIONAL_RISK
    rationale_parts = [
        f"Recommended **{qty_final:.2f}** units ({int(qty_final)} whole) on {candidate.underlying} "
        f"({candidate.silo.value}, {risk_basis}).",
        f"Dollar risk **${dollar_risk:,.0f}**; delta-notional **${delta_notional:,.0f}**.",
        f"Post-trade heat **{heat_after:.1%}**, leverage **{lev_after:.2f}×**.",
    ]

    if qty_final == 0 and max_qty_heat <= 0:
        binding = BindingConstraint.HEAT_CAP
        rationale_parts.append(
            f"**Binding constraint: portfolio heat** — open risk already at or above {cfg.heat_cap:.0%} of equity."
        )
    elif abs(qty_final - max_qty_lev) < 1e-6 and qty_final < qty_pre_cap:
        binding = BindingConstraint.LEVERAGE_CAP
        rationale_parts.append(
            f"**Binding constraint: leverage cap** ({cfg.leverage_cap:.1f}×) — not per-trade fractional risk."
        )
    elif abs(qty_final - max_qty_heat) < 1e-6 and qty_final < qty_pre_cap:
        binding = BindingConstraint.HEAT_CAP
        rationale_parts.append(
            f"**Binding constraint: portfolio heat** ({cfg.heat_cap:.0%}) — not per-trade fractional risk."
        )
    elif qty_l4 < qty_l2 and abs(qty_final - qty_l4) < 1e-6:
        binding = BindingConstraint.KELLY
        rationale_parts.append(
            "**Binding constraint: fractional Kelly** (below fractional-risk baseline)."
        )
    elif candidate.atr and qty_l3 < qty_l2 and abs(qty_final - qty_l3) < 1e-6:
        binding = BindingConstraint.VOLATILITY
        rationale_parts.append(
            "**Binding constraint: volatility normalization** (wider stop / higher ATR)."
        )
    else:
        rationale_parts.append(
            "**Binding constraint: fractional risk** (Kelly and ceilings not tighter)."
        )

    if throttle_mult < 1.0:
        binding = BindingConstraint.DRAWDOWN_THROTTLE

    unit_label = "contracts" if candidate.asset_type != AssetType.STOCK else "shares"
    rationale_parts[0] = rationale_parts[0].replace("units", unit_label)

    return SizeRecommendation(
        recommended_qty=qty_final,
        recommended_qty_int=int(qty_final),
        dollar_risk=dollar_risk,
        delta_notional=delta_notional,
        binding_constraint=binding,
        rationale="\n\n".join(rationale_parts),
        layers=layers,
        heat_after=heat_after,
        leverage_after=lev_after,
        kelly_target_f=kelly_f_used,
        warnings=warnings,
    )


def format_size_recommendation(rec: SizeRecommendation) -> str:
    """Plain-text output for CLI."""
    lines = [
        "Size Recommendation",
        "=" * 40,
        rec.rationale.replace("**", ""),
        "",
        f"Binding: {rec.binding_constraint.value}",
        f"Qty: {rec.recommended_qty:.2f} (whole: {rec.recommended_qty_int})",
        f"Risk: ${rec.dollar_risk:,.0f} | Notional: ${rec.delta_notional:,.0f}",
        f"Heat after: {rec.heat_after:.1%} | Leverage after: {rec.leverage_after:.2f}×",
    ]
    if rec.kelly_target_f is not None:
        lines.append(f"Kelly target f: {rec.kelly_target_f:.3%}")
    lines.append("")
    lines.append("Layers:")
    for layer in rec.layers:
        lines.append(f"  L{layer.layer} {layer.name}: {layer.recommended_qty:.2f} — {layer.detail}")
    if rec.warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in rec.warnings:
            lines.append(f"  • {w}")
    return "\n".join(lines)
