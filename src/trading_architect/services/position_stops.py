"""Position stop overrides and open-R computation — Phase 2."""

from __future__ import annotations

from trading_architect.assembly.positions import futures_multiplier
from trading_architect.config.defaults import OPTION_CONTRACT_MULTIPLIER
from trading_architect.engines.marks import MarksProvider, default_marks_provider
from trading_architect.models.entities import Direction, Position
from trading_architect.store.repository import PositionOverrideRecord, Repository


def _stock_leg_symbol(position: Position) -> str | None:
    for sym, qty in position.leg_net_qty.items():
        if "_" not in sym and qty != 0:
            return sym
    return None


def _underlying_symbol(position: Position) -> str | None:
    sym = _stock_leg_symbol(position)
    if sym:
        return sym
    return position.underlying


def _avg_entry_for_stock(position: Position) -> float | None:
    sym = _stock_leg_symbol(position)
    if sym is None:
        return None
    return position.blended_cost_basis.get(sym)


def _current_mark_for_stock(
    position: Position,
    marks_provider: MarksProvider | None = None,
) -> float | None:
    """Use underlying mark when stock leg exists; avoid blended delta-notional proxy."""
    sym = _underlying_symbol(position)
    if sym is None:
        return None
    stock_sym = _stock_leg_symbol(position)
    qty = position.leg_net_qty.get(stock_sym, 0) if stock_sym else 0
    if stock_sym and qty != 0 and position.current_delta_notional and abs(qty) > 0:
        return abs(position.current_delta_notional / qty)

    provider = marks_provider or default_marks_provider()
    marks = provider.marks_for([sym])
    mark_obj = marks.get(sym)
    if mark_obj is not None and mark_obj.price is not None:
        return float(mark_obj.price)
    if stock_sym:
        return position.blended_cost_basis.get(stock_sym)
    return None


def compute_stop_risk(
    position: Position,
    override: PositionOverrideRecord | None,
) -> float:
    """Dollar risk from persisted stop override."""
    if override is None or override.initial_stop is None:
        return 0.0

    sym = _stock_leg_symbol(position)
    if sym is None:
        return 0.0

    qty = position.leg_net_qty.get(sym, 0)
    if qty == 0:
        return 0.0

    avg_entry = position.blended_cost_basis.get(sym, 0.0)
    mult = futures_multiplier(sym) if position.silo.value == "futures" else 1.0
    return abs(qty) * abs(avg_entry - override.initial_stop) * mult


def _option_open_r(
    position: Position,
    marks_provider: MarksProvider | None = None,
) -> float | None:
    option_legs = [
        (sym, qty, position.blended_cost_basis.get(sym, 0.0))
        for sym, qty in position.leg_net_qty.items()
        if "_" in sym and qty != 0
    ]
    if not option_legs:
        return None

    provider = marks_provider or default_marks_provider()
    symbols = [sym for sym, _, _ in option_legs]
    marks = provider.marks_for(symbols)

    total_cost = 0.0
    total_mtm = 0.0
    for sym, qty, cost_per_share in option_legs:
        premium = abs(cost_per_share) * abs(qty) * OPTION_CONTRACT_MULTIPLIER
        total_cost += premium
        mark_obj = marks.get(sym)
        mark = mark_obj.price if mark_obj else None
        if mark is None:
            mark = abs(cost_per_share)
        total_mtm += float(mark) * abs(qty) * OPTION_CONTRACT_MULTIPLIER

    if total_cost <= 0:
        return None
    return (total_mtm - total_cost) / total_cost


def compute_open_r(
    position: Position,
    override: PositionOverrideRecord | None,
    *,
    marks_provider: MarksProvider | None = None,
) -> float | None:
    """Open R-multiple vs initial stop (direction-aware). Options use premium as R basis."""
    sym = _stock_leg_symbol(position)

    if sym is not None and override and override.initial_stop is not None:
        avg_entry = position.blended_cost_basis.get(sym, 0.0)
        mark = _current_mark_for_stock(position, marks_provider)
        if mark is None or avg_entry == override.initial_stop:
            return None
        r_distance = avg_entry - override.initial_stop
        if r_distance == 0:
            return None
        if position.direction == Direction.LONG:
            return (mark - avg_entry) / r_distance
        return (avg_entry - mark) / (override.initial_stop - avg_entry)

    if position.premium_at_risk > 0:
        return _option_open_r(position, marks_provider)

    return None


def apply_position_overrides(
    positions: list[Position],
    repo: Repository,
    *,
    marks_provider: MarksProvider | None = None,
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
        open_r = compute_open_r(pos, override, marks_provider=marks_provider)
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
