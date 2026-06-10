"""Closed trade entry extraction — per-tranche FIFO round trips for evaluation (PRD §6.3, FR-6.5)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from trading_architect.assembly.positions import futures_multiplier
from trading_architect.config.defaults import DEFAULT_BASE_RISK_F
from trading_architect.ingestion.base import resolve_epoch_id
from trading_architect.models.entities import AssetType, Direction, Side, Silo, TradeEvent


@dataclass
class OpenLot:
    qty: float
    entry_price: float
    entry_time: datetime
    stop_price: float | None
    open_event_id: str
    fees_per_unit: float = 0.0


@dataclass
class ClosedTradeEntry:
    """Single closed round-trip (one tranche) — unit of evaluation."""

    entry_id: str
    silo: Silo
    underlying: str
    symbol: str
    asset_type: AssetType
    direction: Direction
    epoch_id: str | None
    opened_at: datetime
    closed_at: datetime
    quantity: float
    entry_price: float
    exit_price: float
    initial_risk: float
    risk_per_unit: float
    realized_pnl: float
    realized_r: float | None
    open_event_id: str
    close_event_id: str
    risk_basis: str  # stop | premium | proxy_f
    spot_at_entry: float | None = None
    option_delta: float | None = None


def _leg_pnl(
    asset_type: AssetType,
    symbol: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    direction: Direction,
) -> float:
    mult = futures_multiplier(symbol) if asset_type == AssetType.FUTURE else 1.0
    if asset_type == AssetType.OPTION:
        pnl = (exit_price - entry_price) * qty * 100
    else:
        pnl = (exit_price - entry_price) * qty * mult
    if direction == Direction.SHORT:
        pnl = -pnl
    return pnl


def _initial_risk_and_basis(
    asset_type: AssetType,
    symbol: str,
    entry_price: float,
    qty: float,
    stop_price: float | None,
    median_loser_r: float | None = None,
) -> tuple[float, float, str]:
    """Return (initial_risk, risk_per_unit, risk_basis)."""
    if asset_type == AssetType.OPTION:
        rpu = entry_price * 100
        return qty * rpu, rpu, "premium"

    mult = futures_multiplier(symbol) if asset_type == AssetType.FUTURE else 1.0
    if stop_price is not None:
        rpu = abs(entry_price - stop_price) * mult
        if rpu > 0:
            return qty * rpu, rpu, "stop"

    if median_loser_r is not None and median_loser_r > 0:
        return median_loser_r * qty, median_loser_r, "proxy_median_loss"

    notional = qty * entry_price * mult
    initial = notional * DEFAULT_BASE_RISK_F
    rpu = initial / qty if qty else 0.0
    return initial, rpu, "proxy_f"


def _signed_qty(event: TradeEvent) -> float:
    return event.quantity if event.side == Side.BUY else -event.quantity


def extract_closed_entries(events: list[TradeEvent]) -> list[ClosedTradeEntry]:
    """FIFO-match fills into closed round-trip entries, preserving each tranche."""
    sorted_events = sorted(events, key=lambda e: e.timestamp)

    raw: list[dict] = []
    lots_by_leg: dict[tuple[Silo, str, str], list[OpenLot]] = {}
    net_by_leg: dict[tuple[Silo, str, str], float] = {}

    for event in sorted_events:
        leg_key = (event.silo, event.underlying, event.symbol)
        if leg_key not in lots_by_leg:
            lots_by_leg[leg_key] = []
            net_by_leg[leg_key] = 0.0

        lots = lots_by_leg[leg_key]
        net = net_by_leg[leg_key]
        signed = _signed_qty(event)

        # Opening adds exposure in the same direction as net; closing reduces it.
        is_opening = net == 0 or (net > 0 and signed > 0) or (net < 0 and signed < 0)

        if is_opening:
            lots.append(
                OpenLot(
                    qty=event.quantity,
                    entry_price=event.price,
                    entry_time=event.timestamp,
                    stop_price=event.stop_price,
                    open_event_id=event.event_id or event.natural_key,
                    fees_per_unit=event.fees / event.quantity if event.quantity else 0.0,
                )
            )
            net_by_leg[leg_key] = net + signed
            continue

        remaining = event.quantity
        close_direction = Direction.LONG if net > 0 else Direction.SHORT
        while remaining > 0 and lots:
            lot = lots[0]
            matched = min(remaining, lot.qty)
            exit_price = event.price
            pnl = _leg_pnl(
                event.asset_type,
                event.symbol,
                lot.entry_price,
                exit_price,
                matched,
                close_direction,
            )
            fees = lot.fees_per_unit * matched + (
                event.fees * (matched / event.quantity) if event.quantity else 0.0
            )
            pnl -= fees

            raw.append(
                {
                    "silo": event.silo,
                    "underlying": event.underlying,
                    "symbol": event.symbol,
                    "asset_type": event.asset_type,
                    "direction": close_direction,
                    "epoch_id": resolve_epoch_id(lot.entry_time.date()),
                    "opened_at": lot.entry_time,
                    "closed_at": event.timestamp,
                    "quantity": matched,
                    "entry_price": lot.entry_price,
                    "exit_price": exit_price,
                    "realized_pnl": pnl,
                    "stop_price": lot.stop_price,
                    "open_event_id": lot.open_event_id,
                    "close_event_id": event.event_id or event.natural_key,
                    "spot_at_entry": (
                        event.option_spec.strike
                        if event.asset_type == AssetType.OPTION and event.option_spec
                        else lot.entry_price
                        if event.asset_type == AssetType.STOCK
                        else None
                    ),
                    "option_delta": (
                        event.option_spec.delta_at_entry
                        if event.asset_type == AssetType.OPTION and event.option_spec
                        else None
                    ),
                }
            )

            lot.qty -= matched
            remaining -= matched
            if lot.qty <= 0:
                lots.pop(0)

        net_by_leg[leg_key] = net + signed

        if remaining > 0:
            lots.append(
                OpenLot(
                    qty=remaining,
                    entry_price=event.price,
                    entry_time=event.timestamp,
                    stop_price=event.stop_price,
                    open_event_id=event.event_id or event.natural_key,
                    fees_per_unit=event.fees / event.quantity if event.quantity else 0.0,
                )
            )

    # Median loser |R| proxy per silo for entries without stop/premium clarity
    losers_by_silo: dict[Silo, list[float]] = {Silo.STOCK_OPTIONS: [], Silo.FUTURES: []}
    for item in raw:
        if item["realized_pnl"] >= 0:
            continue
        silo = item["silo"]
        asset_type = item["asset_type"]
        qty = item["quantity"]
        entry = item["entry_price"]
        if asset_type == AssetType.OPTION:
            r = qty * entry * 100
        else:
            mult = futures_multiplier(item["symbol"]) if asset_type == AssetType.FUTURE else 1.0
            r = abs(item["realized_pnl"])
            if r <= 0:
                r = qty * entry * mult * DEFAULT_BASE_RISK_F
        if r > 0:
            losers_by_silo[silo].append(r / qty if qty else r)

    def median_loser_r(silo: Silo) -> float | None:
        vals = sorted(losers_by_silo[silo])
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

    entries: list[ClosedTradeEntry] = []
    for item in raw:
        silo = item["silo"]
        initial_risk, rpu, basis = _initial_risk_and_basis(
            item["asset_type"],
            item["symbol"],
            item["entry_price"],
            item["quantity"],
            item["stop_price"],
            median_loser_r(silo),
        )
        from trading_architect.engines.formulas import r_multiple

        realized_r = r_multiple(item["realized_pnl"], initial_risk)
        entries.append(
            ClosedTradeEntry(
                entry_id=str(uuid.uuid4()),
                silo=silo,
                underlying=item["underlying"],
                symbol=item["symbol"],
                asset_type=item["asset_type"],
                direction=item["direction"],
                epoch_id=item["epoch_id"],
                opened_at=item["opened_at"],
                closed_at=item["closed_at"],
                quantity=item["quantity"],
                entry_price=item["entry_price"],
                exit_price=item["exit_price"],
                initial_risk=initial_risk,
                risk_per_unit=rpu,
                realized_pnl=item["realized_pnl"],
                realized_r=realized_r,
                open_event_id=item["open_event_id"],
                close_event_id=item["close_event_id"],
                risk_basis=basis,
                spot_at_entry=item.get("spot_at_entry"),
                option_delta=item.get("option_delta"),
            )
        )

    return entries
