"""Options contract selector — PRD §7.5 (M5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from trading_architect.config.defaults import OPTION_CONTRACT_MULTIPLIER
from trading_architect.config.options_selector import (
    DEFAULT_OPTION_SELECTOR_CONFIG,
    IvAssumption,
    OptionSelectorConfig,
)
from trading_architect.config.sizing import DEFAULT_SIZING_CONFIG, SizingConfig
from trading_architect.engines.formulas import delta_adjusted_notional
from trading_architect.engines.options_pricing import (
    black_scholes_price,
    bs_greeks,
    implied_vol_bisect,
)
from trading_architect.engines.sizing import (
    CandidateTrade,
    SiloExposure,
    SizeRecommendation,
    recommend_size,
)
from trading_architect.models.entities import (
    AssetType,
    ChainContract,
    ChainSnapshot,
    Direction,
    Silo,
)


class OptionDirection(str, Enum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"


@dataclass
class OptionSelectorInput:
    """User-supplied thesis inputs — FR-5.1."""

    underlying: str
    direction: OptionDirection
    target_price: float
    expected_hold_days: int
    path: str = "gradual"  # fast | gradual — affects theta weight emphasis


@dataclass
class ScoredContract:
    """Ranked candidate with repricing, greeks, sizing, and tradeoff notes."""

    rank: int
    contract: ChainContract
    premium: float
    iv_used: float
    projected_value_at_target: float
    projected_return_pct: float
    projected_r_multiple: float
    max_loss: float
    delta_notional_per_contract: float
    composite_score: float
    score_breakdown: dict[str, float]
    size_recommendation: SizeRecommendation | None
    tradeoff_notes: list[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class OptionSelectorResult:
    inputs: OptionSelectorInput
    snapshot: ChainSnapshot
    candidates_evaluated: int
    ranked: list[ScoredContract]
    disclaimer: str = (
        "Modeling aid only: projections assume your stated target and horizon. "
        "Not a probability estimate or trade recommendation."
    )


def _contract_premium(contract: ChainContract) -> float | None:
    if contract.mid is not None and contract.mid > 0:
        return contract.mid
    if contract.ask is not None and contract.ask > 0:
        return contract.ask
    if contract.bid is not None and contract.bid > 0:
        return contract.bid
    return None


def _resolve_iv(
    contract: ChainContract,
    snapshot: ChainSnapshot,
    premium: float,
) -> float:
    if contract.iv is not None and contract.iv > 0:
        iv = contract.iv
        return iv / 100.0 if iv > 3 else iv
    t_years = max(contract.dte, 1) / 365.0
    solved = implied_vol_bisect(
        premium,
        snapshot.spot_price,
        contract.strike,
        t_years,
        snapshot.risk_free_rate,
        contract.right,
    )
    return solved if solved else 0.35


def _contract_greeks(
    contract: ChainContract,
    snapshot: ChainSnapshot,
    premium: float,
) -> dict[str, float]:
    """Return greeks from chain or fill each missing greek via Black-Scholes."""
    if (
        contract.delta is not None
        and contract.theta is not None
        and contract.vega is not None
        and contract.gamma is not None
    ):
        return {
            "delta": contract.delta,
            "theta": contract.theta,
            "vega": contract.vega,
            "gamma": contract.gamma,
        }

    iv = _resolve_iv(contract, snapshot, premium)
    t_years = max(contract.dte, 1) / 365.0
    computed = bs_greeks(
        snapshot.spot_price,
        contract.strike,
        t_years,
        snapshot.risk_free_rate,
        iv,
        contract.right,
    )
    return {
        "delta": contract.delta if contract.delta is not None else computed["delta"],
        "theta": contract.theta if contract.theta is not None else computed["theta"],
        "vega": contract.vega if contract.vega is not None else computed["vega"],
        "gamma": contract.gamma if contract.gamma is not None else computed["gamma"],
    }


def _iv_at_target(base_iv: float, config: OptionSelectorConfig) -> float:
    if config.iv_assumption == IvAssumption.DECLINE:
        return max(0.05, base_iv * (1.0 - config.iv_decline_pct))
    return base_iv


def reprice_at_target(
    contract: ChainContract,
    snapshot: ChainSnapshot,
    target_price: float,
    expected_hold_days: int,
    premium: float,
    config: OptionSelectorConfig,
) -> tuple[float, float, float, float]:
    """FR-5.3 — BS value at target with reduced DTE."""
    base_iv = _resolve_iv(contract, snapshot, premium)
    iv_target = _iv_at_target(base_iv, config)
    dte_remaining = max(0, contract.dte - expected_hold_days)
    t_years = dte_remaining / 365.0
    projected = black_scholes_price(
        target_price,
        contract.strike,
        t_years,
        snapshot.risk_free_rate,
        iv_target,
        contract.right,
    )
    ret_pct = ((projected - premium) / premium * 100.0) if premium > 0 else 0.0
    r_mult = (projected - premium) / premium if premium > 0 else 0.0
    return projected, ret_pct, r_mult, iv_target


def enumerate_candidates(
    snapshot: ChainSnapshot,
    inputs: OptionSelectorInput,
    config: OptionSelectorConfig,
) -> list[ChainContract]:
    """FR-5.2 — strikes near target + DTE band from horizon."""
    right = "C" if inputs.direction == OptionDirection.LONG_CALL else "P"
    min_dte = max(config.min_dte, inputs.expected_hold_days + config.dte_buffer_days)
    max_dte = config.max_dte

    pool = [
        c
        for c in snapshot.contracts
        if c.right == right
        and c.dte >= inputs.expected_hold_days
        and min_dte <= c.dte <= max_dte
        and _contract_premium(c)
    ]
    if not pool:
        return []

    strikes = sorted({c.strike for c in pool})
    target = inputs.target_price

    if inputs.direction == OptionDirection.LONG_CALL:
        at_or_below = [s for s in strikes if s <= target]
        if not at_or_below:
            chosen = strikes[: config.strikes_below_target + 1]
        else:
            anchor = max(at_or_below)
            idx = strikes.index(anchor)
            start = max(0, idx - config.strikes_below_target)
            chosen = strikes[start : idx + 1]
    else:
        at_or_above = [s for s in strikes if s >= target]
        if not at_or_above:
            chosen = strikes[-(config.strikes_below_target + 1) :]
        else:
            anchor = min(at_or_above)
            idx = strikes.index(anchor)
            end = min(len(strikes), idx + config.strikes_below_target + 1)
            chosen = strikes[idx:end]

    chosen_set = set(chosen)
    by_strike_expiry: dict[tuple[float, str], ChainContract] = {}
    for c in pool:
        if c.strike not in chosen_set:
            continue
        key = (c.strike, c.expiry.isoformat())
        prev = by_strike_expiry.get(key)
        if prev is None:
            by_strike_expiry[key] = c
            continue
        prev_mid = _contract_premium(prev) or 0
        cur_mid = _contract_premium(c) or 0
        if cur_mid > 0 and (
            prev_mid <= 0
            or abs(c.dte - inputs.expected_hold_days) < abs(prev.dte - inputs.expected_hold_days)
        ):
            by_strike_expiry[key] = c

    return list(by_strike_expiry.values())


def _breakeven(spot: float, strike: float, premium: float, right: str) -> float:
    if right == "C":
        return strike + premium
    return strike - premium


def _score_dimension(values: list[float], higher_is_better: bool) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    normed = [(v - lo) / (hi - lo) for v in values]
    if not higher_is_better:
        normed = [1.0 - n for n in normed]
    return normed


def _build_tradeoff_notes(
    contract: ChainContract,
    spot: float,
    premium: float,
    projected_return: float,
    inputs: OptionSelectorInput,
) -> list[str]:
    notes: list[str] = []
    moneyness = (
        "OTM"
        if (contract.right == "C" and contract.strike > spot)
        or (contract.right == "P" and contract.strike < spot)
        else "ITM/ATM"
    )

    if contract.strike < inputs.target_price and contract.right == "C":
        notes.append(
            f"Strike ${contract.strike:.0f} is below target ${inputs.target_price:.0f} — "
            "higher delta and premium, less leverage vs farther OTM."
        )
    elif contract.strike > inputs.target_price and contract.right == "P":
        notes.append(
            f"Strike ${contract.strike:.0f} is above target ${inputs.target_price:.0f} — "
            "more intrinsic sensitivity, higher cost."
        )
    else:
        notes.append(
            f"Strike ${contract.strike:.0f} near target — balances leverage and move required ({moneyness})."
        )

    if contract.dte > inputs.expected_hold_days + 60:
        notes.append(
            f"DTE {contract.dte} gives cushion past {inputs.expected_hold_days}d hold — lower theta pressure."
        )
    elif contract.dte < inputs.expected_hold_days + 30:
        notes.append(
            f"DTE {contract.dte} is tight vs {inputs.expected_hold_days}d hold — elevated end-of-life gamma/theta risk."
        )

    if projected_return > 100:
        notes.append(
            f"Projected return at target: {projected_return:.0f}% (deterministic what-if, not expected value)."
        )
    elif projected_return < 0:
        notes.append(
            "At your target, model value is below premium paid — check IV assumption or target."
        )

    return notes


def rank_contracts(
    snapshot: ChainSnapshot,
    inputs: OptionSelectorInput,
    exposure: SiloExposure,
    *,
    config: OptionSelectorConfig | None = None,
    sizing_config: SizingConfig | None = None,
    events=None,
    top_n: int = 8,
) -> OptionSelectorResult:
    """Score, rank, and attach sizing for candidate contracts."""
    cfg = config or DEFAULT_OPTION_SELECTOR_CONFIG
    sz_cfg = sizing_config or DEFAULT_SIZING_CONFIG
    weights = cfg.scoring_weights.normalized()

    candidates = enumerate_candidates(snapshot, inputs, cfg)
    if not candidates:
        return OptionSelectorResult(
            inputs=inputs,
            snapshot=snapshot,
            candidates_evaluated=0,
            ranked=[],
        )

    raw_rows: list[dict] = []
    for contract in candidates:
        premium = _contract_premium(contract)
        if premium is None or premium <= 0:
            continue

        projected, ret_pct, r_mult, iv_used = reprice_at_target(
            contract, snapshot, inputs.target_price, inputs.expected_hold_days, premium, cfg
        )
        greeks = _contract_greeks(contract, snapshot, premium)
        delta = abs(greeks["delta"])
        delta_notional = delta_adjusted_notional(0, snapshot.spot_price, [(1.0, delta)])
        cost_eff = delta_notional / (premium * OPTION_CONTRACT_MULTIPLIER) if premium > 0 else 0

        theta = greeks["theta"]
        theta_drag = abs(theta) * inputs.expected_hold_days
        vega = abs(greeks["vega"])
        iv_penalty = 0.0
        if snapshot.iv_percentile is not None:
            iv_penalty = snapshot.iv_percentile
        elif snapshot.iv_rank is not None:
            iv_penalty = snapshot.iv_rank

        be = _breakeven(snapshot.spot_price, contract.strike, premium, contract.right)
        if inputs.direction == OptionDirection.LONG_CALL:
            cushion = (inputs.target_price - be) / snapshot.spot_price if snapshot.spot_price else 0
        else:
            cushion = (be - inputs.target_price) / snapshot.spot_price if snapshot.spot_price else 0

        delta_dist = abs(delta - cfg.target_delta)

        raw_rows.append(
            {
                "contract": contract,
                "premium": premium,
                "iv_used": iv_used,
                "projected": projected,
                "ret_pct": ret_pct,
                "r_mult": r_mult,
                "cost_eff": cost_eff,
                "ret_pct_score": ret_pct,
                "theta_drag": theta_drag,
                "vega_iv": vega + iv_penalty,
                "delta_dist": delta_dist,
                "cushion": cushion,
            }
        )

    if not raw_rows:
        return OptionSelectorResult(
            inputs=inputs,
            snapshot=snapshot,
            candidates_evaluated=len(candidates),
            ranked=[],
        )

    dims = {
        "cost_eff": _score_dimension([r["cost_eff"] for r in raw_rows], True),
        "payoff": _score_dimension([r["ret_pct_score"] for r in raw_rows], True),
        "theta": _score_dimension([r["theta_drag"] for r in raw_rows], False),
        "vega": _score_dimension([r["vega_iv"] for r in raw_rows], False),
        "delta": _score_dimension([r["delta_dist"] for r in raw_rows], False),
        "cushion": _score_dimension([r["cushion"] for r in raw_rows], True),
    }

    path_theta_mult = 1.25 if inputs.path.lower() == "fast" else 1.0

    scored: list[tuple[float, dict]] = []
    for i, row in enumerate(raw_rows):
        breakdown = {
            "cost_efficiency": dims["cost_eff"][i],
            "projected_payoff": dims["payoff"][i],
            "theta_drag": dims["theta"][i],
            "vega_iv_risk": dims["vega"][i],
            "delta_fit": dims["delta"][i],
            "breakeven_cushion": dims["cushion"][i],
        }
        composite = (
            weights.cost_efficiency * breakdown["cost_efficiency"]
            + weights.projected_payoff * breakdown["projected_payoff"]
            + weights.theta_drag * breakdown["theta_drag"] * path_theta_mult
            + weights.vega_iv_risk * breakdown["vega_iv_risk"]
            + weights.delta_responsiveness * breakdown["delta_fit"]
            + weights.breakeven_cushion * breakdown["breakeven_cushion"]
        )
        scored.append((composite, {**row, "breakdown": breakdown, "composite": composite}))

    scored.sort(key=lambda x: x[0], reverse=True)

    ranked: list[ScoredContract] = []
    for rank_idx, (_, row) in enumerate(scored[:top_n], start=1):
        contract = row["contract"]
        premium = row["premium"]
        greeks = _contract_greeks(contract, snapshot, premium)
        delta = abs(greeks["delta"])

        size_rec = recommend_size(
            CandidateTrade(
                silo=Silo.STOCK_OPTIONS,
                underlying=inputs.underlying.upper(),
                direction=Direction.LONG,
                asset_type=AssetType.OPTION,
                entry_price=premium,
                premium_per_contract=premium,
                spot_price=snapshot.spot_price,
                option_delta=delta if contract.right == "C" else -delta,
                target_prices=(inputs.target_price,),
                projected_premium_at_targets=(row["projected"],),
            ),
            exposure,
            config=sz_cfg,
            events=events,
        )

        notes = _build_tradeoff_notes(
            contract, snapshot.spot_price, premium, row["ret_pct"], inputs
        )
        if size_rec.binding_constraint.value in ("heat_cap", "leverage_cap"):
            notes.append(
                f"Sizing capped by {size_rec.binding_constraint.value.replace('_', ' ')} "
                f"({size_rec.recommended_qty_int} contracts)."
            )

        rationale = (
            f"#{rank_idx} {contract.right} ${contract.strike:.0f} "
            f"exp {contract.expiry} ({contract.dte} DTE) — "
            f"premium ${premium:.2f}, score {row['composite']:.2f}. "
            f"At target ${inputs.target_price:.2f}: model value ${row['projected']:.2f} "
            f"({row['ret_pct']:+.0f}% return, {row['r_mult']:+.2f}R). "
            f"Max loss = premium (${premium * OPTION_CONTRACT_MULTIPLIER:,.0f}/contract)."
        )

        ranked.append(
            ScoredContract(
                rank=rank_idx,
                contract=contract,
                premium=premium,
                iv_used=row["iv_used"],
                projected_value_at_target=row["projected"],
                projected_return_pct=row["ret_pct"],
                projected_r_multiple=row["r_mult"],
                max_loss=premium * OPTION_CONTRACT_MULTIPLIER,
                delta_notional_per_contract=delta_adjusted_notional(
                    0, snapshot.spot_price, [(1.0, delta)]
                ),
                composite_score=row["composite"],
                score_breakdown=row["breakdown"],
                size_recommendation=size_rec,
                tradeoff_notes=notes,
                rationale=rationale,
            )
        )

    return OptionSelectorResult(
        inputs=inputs,
        snapshot=snapshot,
        candidates_evaluated=len(candidates),
        ranked=ranked,
    )


def format_option_selector_result(result: OptionSelectorResult) -> str:
    """Plain-text CLI output."""
    lines = [
        "Options Contract Selector",
        "=" * 40,
        f"{result.inputs.underlying} {result.inputs.direction.value} → "
        f"target ${result.inputs.target_price:.2f}, hold {result.inputs.expected_hold_days}d",
        f"Spot ${result.snapshot.spot_price:.2f} · "
        f"{result.candidates_evaluated} candidates evaluated · "
        f"{len(result.ranked)} ranked",
        "",
        result.disclaimer,
        "",
    ]
    if not result.ranked:
        lines.append(
            "No contracts matched filters. Import a chain CSV with strikes/expiries in range."
        )
        return "\n".join(lines)

    for item in result.ranked:
        c = item.contract
        lines.append(f"#{item.rank}  {c.right} ${c.strike:.0f}  {c.expiry}  ({c.dte} DTE)")
        lines.append(f"  {item.rationale}")
        if item.size_recommendation:
            sr = item.size_recommendation
            lines.append(
                f"  Size: {sr.recommended_qty_int} contracts · "
                f"risk ${sr.dollar_risk:,.0f} · leverage after {sr.leverage_after:.2f}×"
            )
        greek_parts = []
        if c.delta is not None:
            greek_parts.append(f"δ={c.delta:.2f}")
        if c.theta is not None:
            greek_parts.append(f"θ={c.theta:.2f}")
        if c.vega is not None:
            greek_parts.append(f"ν={c.vega:.2f}")
        if greek_parts:
            lines.append(f"  Greeks: {', '.join(greek_parts)}")
        for note in item.tradeoff_notes:
            lines.append(f"  • {note}")
        lines.append("")
    return "\n".join(lines)
