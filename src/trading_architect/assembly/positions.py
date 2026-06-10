"""Position assembly from TradeEvents per PRD §6.3."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from trading_architect.config.defaults import FUTURES_POINT_VALUES
from trading_architect.engines.formulas import delta_adjusted_notional
from trading_architect.ingestion.parsing import is_assemblable_event
from trading_architect.models.entities import (
    AssetType,
    Direction,
    Position,
    PositionStatus,
    Side,
    Silo,
    TradeEvent,
)


@dataclass
class LegState:
    net_qty: float = 0.0
    cost_basis: float = 0.0
    premium_at_risk: float = 0.0
    stop_risk: float = 0.0
    asset_type: AssetType | None = None
    option_delta: float | None = None
    account_qty: dict[str, float] = field(default_factory=dict)


@dataclass
class PositionBuilder:
    silo: Silo
    underlying: str
    direction: Direction
    event_ids: list[str] = field(default_factory=list)
    legs: dict[str, LegState] = field(default_factory=lambda: defaultdict(LegState))
    opened_at: object | None = None
    closed_at: object | None = None
    realized_pnl: float = 0.0
    epoch_id: str | None = None


def infer_direction(event: TradeEvent) -> Direction:
    if event.asset_type == AssetType.OPTION and event.option_spec:
        if event.option_spec.right == "C":
            return Direction.LONG if event.side == Side.BUY else Direction.SHORT
        return Direction.SHORT if event.side == Side.BUY else Direction.LONG

    if event.side == Side.BUY:
        return Direction.LONG
    return Direction.SHORT


def futures_multiplier(symbol: str) -> float:
    root = symbol[:3]
    for key, val in FUTURES_POINT_VALUES.items():
        if symbol.upper().startswith(key):
            return val
    return FUTURES_POINT_VALUES.get(root, 1.0)


def _leg_pnl_close(
    event: TradeEvent,
    leg: LegState,
    closed_qty: float,
    prev_qty: float,
) -> float:
    if event.asset_type == AssetType.FUTURE:
        mult = futures_multiplier(event.symbol)
        pnl = (event.price - leg.cost_basis) * closed_qty * mult
        if prev_qty < 0:
            pnl = -pnl
    elif event.asset_type == AssetType.OPTION:
        pnl = (event.price - leg.cost_basis) * closed_qty * 100
        if prev_qty < 0:
            pnl = -pnl
    else:
        pnl = (event.price - leg.cost_basis) * closed_qty
        if prev_qty < 0:
            pnl = -pnl
    return pnl


def apply_event(builder: PositionBuilder, event: TradeEvent) -> None:
    leg_key = event.symbol
    leg = builder.legs[leg_key]
    leg.asset_type = event.asset_type
    if event.asset_type == AssetType.OPTION and event.option_spec:
        if event.option_spec.delta_at_entry is not None:
            leg.option_delta = event.option_spec.delta_at_entry
    signed = event.signed_quantity()
    cash_flow = -signed * event.price
    if event.asset_type == AssetType.FUTURE:
        cash_flow *= futures_multiplier(event.symbol)

    prev_qty = leg.net_qty
    new_qty = prev_qty + signed

    if prev_qty == 0:
        leg.cost_basis = event.price
    elif (prev_qty > 0 and signed > 0) or (prev_qty < 0 and signed < 0):
        total_cost = abs(prev_qty) * leg.cost_basis + abs(signed) * event.price
        leg.cost_basis = total_cost / abs(new_qty) if new_qty else 0.0
    elif new_qty == 0:
        pnl = _leg_pnl_close(event, leg, abs(signed), prev_qty)
        builder.realized_pnl += pnl - event.fees
        builder.closed_at = event.timestamp
        leg.cost_basis = 0.0
    else:
        # Partial close — proportional P&L; overshoot opens opposite-direction lot
        closed_qty = min(abs(signed), abs(prev_qty))
        pnl = _leg_pnl_close(event, leg, closed_qty, prev_qty)
        builder.realized_pnl += pnl - event.fees * (
            closed_qty / event.quantity if event.quantity else 1.0
        )

        overshoot = abs(signed) - closed_qty
        if overshoot > 0:
            leg.net_qty = overshoot if signed > 0 else -overshoot
            leg.cost_basis = event.price
            if event.asset_type == AssetType.OPTION and event.option_spec:
                leg.option_delta = event.option_spec.delta_at_entry
            new_qty = leg.net_qty
        elif new_qty == 0:
            builder.closed_at = event.timestamp
            leg.cost_basis = 0.0

    leg.net_qty = new_qty

    acct = event.account
    leg.account_qty[acct] = leg.account_qty.get(acct, 0.0) + signed
    if leg.net_qty == 0:
        leg.account_qty.clear()
    else:
        for account, qty in list(leg.account_qty.items()):
            if qty == 0:
                del leg.account_qty[account]

    if leg.net_qty == 0:
        leg.premium_at_risk = 0.0
        leg.stop_risk = 0.0
    elif event.asset_type == AssetType.OPTION and leg.net_qty > 0:
        leg.premium_at_risk = leg.net_qty * leg.cost_basis * 100
    elif event.asset_type == AssetType.STOCK and leg.net_qty != 0 and event.stop_price:
        leg.stop_risk = abs(leg.net_qty) * abs(leg.cost_basis - event.stop_price)
    elif event.asset_type == AssetType.FUTURE and leg.net_qty != 0 and event.stop_price:
        mult = futures_multiplier(event.symbol)
        leg.stop_risk = abs(leg.net_qty) * abs(leg.cost_basis - event.stop_price) * mult

    builder.event_ids.append(event.event_id or event.natural_key)
    if builder.opened_at is None:
        builder.opened_at = event.timestamp
    builder.epoch_id = event.epoch_id


def _latest_marks(events: list[TradeEvent]) -> tuple[dict[str, float], dict[str, float]]:
    """Return (latest_price_by_symbol, latest_spot_by_underlying) from events."""
    latest_price: dict[str, float] = {}
    latest_spot: dict[str, float] = {}
    for event in sorted(events, key=lambda e: e.timestamp):
        latest_price[event.symbol] = event.price
        if event.asset_type == AssetType.STOCK:
            latest_spot[event.underlying] = event.price
    return latest_price, latest_spot


def _compute_delta_notional(
    builder: PositionBuilder,
    spot_by_underlying: dict[str, float],
    latest_price_by_symbol: dict[str, float],
) -> float:
    spot = spot_by_underlying.get(builder.underlying)
    if spot is None:
        spot = latest_price_by_symbol.get(builder.underlying, 0.0)

    stock_qty = 0.0
    option_contracts: list[tuple[float, float]] = []
    future_notional = 0.0

    for sym, leg in builder.legs.items():
        if leg.net_qty == 0:
            continue
        if leg.asset_type == AssetType.STOCK:
            stock_qty += leg.net_qty
        elif leg.asset_type == AssetType.OPTION:
            delta = leg.option_delta if leg.option_delta is not None else 0.5
            if leg.net_qty < 0:
                delta = -delta
            option_contracts.append((abs(leg.net_qty), delta))
        elif leg.asset_type == AssetType.FUTURE:
            mult = futures_multiplier(sym)
            future_notional += abs(leg.net_qty) * leg.cost_basis * mult

    return delta_adjusted_notional(stock_qty, spot, option_contracts) + future_notional


def builder_to_position(
    builder: PositionBuilder,
    *,
    spot_by_underlying: dict[str, float] | None = None,
    latest_price_by_symbol: dict[str, float] | None = None,
) -> Position:
    open_qty = sum(abs(leg.net_qty) for leg in builder.legs.values() if leg.net_qty != 0)
    status = PositionStatus.OPEN if open_qty > 0 else PositionStatus.CLOSED

    premium = sum(leg.premium_at_risk for leg in builder.legs.values())
    stop_risk = sum(leg.stop_risk for leg in builder.legs.values())

    cost_basis = {sym: leg.cost_basis for sym, leg in builder.legs.items() if leg.net_qty != 0}
    leg_net_qty = {sym: leg.net_qty for sym, leg in builder.legs.items() if leg.net_qty != 0}
    account_leg_qty = {
        sym: dict(leg.account_qty)
        for sym, leg in builder.legs.items()
        if leg.net_qty != 0 and leg.account_qty
    }

    delta_notional = 0.0
    if (
        status == PositionStatus.OPEN
        and spot_by_underlying is not None
        and latest_price_by_symbol is not None
    ):
        delta_notional = _compute_delta_notional(
            builder, spot_by_underlying, latest_price_by_symbol
        )

    closed_at = builder.closed_at
    if status == PositionStatus.CLOSED and builder.event_ids:
        closed_at = closed_at or builder.opened_at

    return Position(
        position_id=str(uuid.uuid4()),
        silo=builder.silo,
        underlying=builder.underlying,
        direction=builder.direction,
        status=status,
        component_event_ids=builder.event_ids,
        blended_cost_basis=cost_basis,
        leg_net_qty=leg_net_qty,
        account_leg_qty=account_leg_qty,
        current_delta_notional=delta_notional,
        premium_at_risk=premium,
        stop_risk=stop_risk,
        total_dollar_risk=premium + stop_risk,
        epoch_id=builder.epoch_id,
        realized_pnl=builder.realized_pnl,
        opened_at=builder.opened_at,
        closed_at=closed_at if status == PositionStatus.CLOSED else None,
    )


def assemble_positions(events: list[TradeEvent]) -> list[Position]:
    """Group events into logical positions by silo + underlying.

    A fully closed book starts a new position on the next open so long→short
    reversals do not merge into one record (PRD §6.3).
    """
    latest_price_by_symbol, spot_by_underlying = _latest_marks(events)
    sorted_events = sorted(events, key=lambda e: e.timestamp)
    sorted_events = [e for e in sorted_events if is_assemblable_event(e)]
    active: dict[tuple[Silo, str], PositionBuilder] = {}
    finished: list[PositionBuilder] = []

    for event in sorted_events:
        key = (event.silo, event.underlying)
        if key not in active:
            active[key] = PositionBuilder(
                silo=event.silo,
                underlying=event.underlying,
                direction=infer_direction(event),
            )
        apply_event(active[key], event)
        open_qty = sum(abs(leg.net_qty) for leg in active[key].legs.values() if leg.net_qty != 0)
        if open_qty == 0:
            finished.append(active.pop(key))

    results: list[Position] = []
    for builder in finished + list(active.values()):
        pos = builder_to_position(
            builder,
            spot_by_underlying=spot_by_underlying,
            latest_price_by_symbol=latest_price_by_symbol,
        )
        # Set direction from net stock/future qty when available
        net_stock = sum(leg.net_qty for sym, leg in builder.legs.items() if leg.net_qty != 0)
        if net_stock > 0:
            pos.direction = Direction.LONG
        elif net_stock < 0:
            pos.direction = Direction.SHORT
        results.append(pos)

    return results


def enrich_positions_with_marks(
    positions: list[Position],
    marks_provider,
    events: list[TradeEvent] | None = None,
) -> list[Position]:
    """Post-assembly enrichment: live delta-notional and zero closed-leg risk (H-1, M-1)."""
    from trading_architect.engines.formulas import delta_adjusted_notional
    from trading_architect.models.entities import PositionStatus

    events = events or []
    events_by_symbol = {e.symbol: e for e in sorted(events, key=lambda ev: ev.timestamp)}

    open_positions = [p for p in positions if p.status == PositionStatus.OPEN]
    symbols_needed: set[str] = set()
    underlyings: set[str] = set()
    for pos in open_positions:
        underlyings.add(pos.underlying)
        for sym in pos.leg_net_qty:
            symbols_needed.add(sym)
    for u in underlyings:
        symbols_needed.add(u)

    marks = marks_provider.marks_for(list(symbols_needed)) if symbols_needed else {}

    enriched: list[Position] = []
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            pos = pos.model_copy(
                update={
                    "premium_at_risk": 0.0,
                    "stop_risk": 0.0,
                    "total_dollar_risk": 0.0,
                    "current_delta_notional": 0.0,
                }
            )
            enriched.append(pos)
            continue

        spot_mark = marks.get(pos.underlying)
        spot = spot_mark.price if spot_mark else None

        stock_qty = 0.0
        option_contracts: list[tuple[float, float]] = []

        for sym, qty in pos.leg_net_qty.items():
            if qty == 0:
                continue
            event = events_by_symbol.get(sym)
            asset_type = event.asset_type if event else None
            if asset_type is None and ("_" in sym and sym.split("_")[-2] in ("C", "P")):
                asset_type = AssetType.OPTION
            elif asset_type is None:
                asset_type = AssetType.STOCK

            if asset_type == AssetType.STOCK:
                stock_qty += qty
            elif asset_type == AssetType.OPTION:
                mark = marks.get(sym)
                leg_spot = spot
                if leg_spot is None and mark:
                    leg_spot = mark.price  # fallback unlikely for options
                delta = mark.delta if mark and mark.delta is not None else None
                if delta is None and event and event.option_spec:
                    delta = event.option_spec.delta_at_entry
                if delta is None:
                    delta = 0.5
                if qty < 0:
                    delta = -abs(delta)
                else:
                    delta = abs(delta)
                option_contracts.append((abs(qty), delta))

        if spot is None:
            for sym, qty in pos.leg_net_qty.items():
                if qty != 0 and sym in marks:
                    spot = marks[sym].price
                    break
            if spot is None:
                for sym, cost in pos.blended_cost_basis.items():
                    if sym == pos.underlying:
                        spot = cost
                        break

        delta_notional = (
            delta_adjusted_notional(stock_qty, spot or 0.0, option_contracts) if spot else 0.0
        )
        enriched.append(pos.model_copy(update={"current_delta_notional": delta_notional}))

    return enriched
