"""Position stop overrides and open-R computation — Phase 2."""

from __future__ import annotations

from trading_architect.assembly.positions import futures_multiplier
from trading_architect.config.defaults import OPTION_CONTRACT_MULTIPLIER
from trading_architect.models.entities import Direction, Position
from trading_architect.store.repository import PositionOverrideRecord, Repository


def _stock_leg_symbol(position: Position) -> str | None:
    for sym, qty in position.leg_net_qty.items():
        if "_" not in sym and qty != 0:
            return sym
    return None


def _avg_entry_for_stock(position: Position) -> float | None:
    sym = _stock_leg_symbol(position)
    if sym is None:
        return None
    return position.blended_cost_basis.get(sym)


def _current_mark_for_stock(position: Position) -> float | None:
    """Use delta-notional / qty as mark proxy when enriched."""
    sym = _stock_leg_symbol(position)
    if sym is None:
        return None
    qty = position.leg_net_qty.get(sym, 0)
    if qty == 0:
        return None
    if position.current_delta_notional and abs(qty) > 0:
        return abs(position.current_delta_notional / qty)
    return position.blended_cost_basis.get(sym)


def compute_stop_risk(
    position: Position,
    override: PositionOverrideRecord | None,
) -> float:
    """Dollar risk from persisted stop override."""
    if override is None or override.initial_stop is None:
        return 0.0

    sym = _stock_leg_symbol(position)
    if sym is None:
        return position.premium_at_risk

    qty = position.leg_net_qty.get(sym, 0)
    if qty == 0:
        return 0.0

    avg_entry = position.blended_cost_basis.get(sym, 0.0)
    mult = futures_multiplier(sym) if position.silo.value == "futures" else 1.0
    return abs(qty) * abs(avg_entry - override.initial_stop) * mult


def compute_open_r(
    position: Position,
    override: PositionOverrideRecord | None,
) -> float | None:
    """Open R-multiple vs initial stop (direction-aware). Options use premium as R basis."""
    sym = _stock_leg_symbol(position)

    if sym is not None and override and override.initial_stop is not None:
        avg_entry = position.blended_cost_basis.get(sym, 0.0)
        mark = _current_mark_for_stock(position)
        if mark is None or avg_entry == override.initial_stop:
            return None
        r_distance = avg_entry - override.initial_stop
        if r_distance == 0:
            return None
        if position.direction == Direction.LONG:
            return (mark - avg_entry) / r_distance
        return (avg_entry - mark) / (override.initial_stop - avg_entry)

    if position.premium_at_risk > 0 and position.current_delta_notional:
        option_legs = [
            (sym, qty, position.blended_cost_basis.get(sym, 0.0))
            for sym, qty in position.leg_net_qty.items()
            if "_" in sym and qty > 0
        ]
        if not option_legs:
            return None
        total_cost = sum(abs(cost) * abs(qty) * OPTION_CONTRACT_MULTIPLIER for _, qty, cost in option_legs)
        if total_cost <= 0:
            return None
        mtm = sum(position.leg_net_qty.get(s, 0) * position.blended_cost_basis.get(s, 0.0) for s in position.leg_net_qty if "_" in s)
        unrealized = position.current_delta_notional - abs(mtm) * OPTION_CONTRACT_MULTIPLIER if mtm else 0.0
        return unrealized / total_cost

    return None


def apply_position_overrides(
    positions: list[Position],
    repo: Repository,
) -> list[Position]:
    """Enrich positions with stop overrides, stop_risk, open_r, and total_dollar_risk."""
    overrides = {(o.symbol, o.silo): o for o in repo.list_position_overrides()}
    enriched: list[Position] = []

    for pos in positions:
        key = (pos.underlying, pos.silo)
        override = overrides.get(key)
        if override is None:
            stock_sym = _stock_leg_symbol(pos)
            if stock_sym:
                override = overrides.get((stock_sym, pos.silo))

        stop_risk = compute_stop_risk(pos, override)
        open_r = compute_open_r(pos, override)
        total_risk = pos.premium_at_risk + stop_risk

        enriched.append(
            pos.model_copy(
                update={
                    "stop_risk": stop_risk,
                    "open_r": open_r,
                    "total_dollar_risk": total_risk,
                }
            )
        )

    return enriched


def has_stop(position: Position, repo: Repository) -> bool:
    """True when a stop override exists for this position."""
    override = repo.get_position_override(position.underlying, position.silo)
    if override and override.initial_stop is not None:
        return True
    sym = _stock_leg_symbol(position)
    if sym:
        override = repo.get_position_override(sym, position.silo)
        return override is not None and override.initial_stop is not None
    return position.premium_at_risk > 0
