"""Marked-to-market equity reconstruction — PRD A.6."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Callable

import pandas as pd

from trading_architect.assembly.entries import ClosedTradeEntry
from trading_architect.assembly.positions import futures_multiplier
from trading_architect.config.defaults import OPTION_CONTRACT_MULTIPLIER
from trading_architect.models.entities import AssetType, Position, PositionStatus, Silo, TradeEvent


@dataclass(frozen=True)
class MarkFallbackStats:
    """Counts of fallback pricing used in MTM equity."""

    legs_at_cost_basis: int
    delta_iv_fallbacks: int
    missing_mark_symbols: tuple[str, ...]


class EquitySnapshot:
    silo: Silo
    asof: pd.Timestamp
    realized_cash: float
    open_mark: float
    total_equity: float


def _latest_marks_by_symbol(events: list[TradeEvent]) -> dict[str, float]:
    marks: dict[str, float] = {}
    for event in sorted(events, key=lambda e: e.timestamp):
        marks[event.symbol] = event.price
    return marks


def _open_position_symbols(positions: list[Position]) -> list[str]:
    symbols: set[str] = set()
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        for sym, qty in pos.leg_net_qty.items():
            if qty != 0:
                symbols.add(sym)
        symbols.add(pos.underlying)
    return list(symbols)


def _marks_with_provider(
    events: list[TradeEvent],
    positions: list[Position],
    marks_provider=None,
) -> tuple[dict[str, float], dict | None]:
    """Event-based marks overlaid with live marks when a provider is configured."""
    marks = _latest_marks_by_symbol(events)
    if marks_provider is None:
        return marks, None
    live = marks_provider.marks_for(_open_position_symbols(positions))
    for sym, mark in live.items():
        marks[sym] = mark.price
    return marks, live


def mark_fallback_stats_for_book(
    events: list[TradeEvent],
    positions: list[Position],
    marks_provider=None,
) -> MarkFallbackStats:
    """Fallback counts for all open positions using the latest marks provider."""
    open_positions = [p for p in positions if p.status == PositionStatus.OPEN]
    if not open_positions:
        return MarkFallbackStats(0, 0, ())

    all_events = [e for e in events if e.silo in {p.silo for p in open_positions}]
    marks, live = _marks_with_provider(all_events, open_positions, marks_provider)
    return compute_mark_fallback_stats(open_positions, marks, live)


def compute_mark_fallback_stats(
    positions: list[Position],
    marks_by_symbol: dict[str, float],
    live_marks: dict | None = None,
) -> MarkFallbackStats:
    """Count open legs priced at cost basis and delta IV fallbacks."""
    missing: list[str] = []
    legs_at_cost = 0
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        for symbol, qty in pos.leg_net_qty.items():
            if qty == 0:
                continue
            if symbol not in marks_by_symbol:
                legs_at_cost += 1
                missing.append(symbol)

    delta_iv = 0
    if live_marks:
        delta_iv = sum(1 for mark in live_marks.values() if getattr(mark, "iv_fallback", False))

    return MarkFallbackStats(
        legs_at_cost_basis=legs_at_cost,
        delta_iv_fallbacks=delta_iv,
        missing_mark_symbols=tuple(sorted(set(missing))),
    )


def _leg_asset_type(
    symbol: str, position: Position, events_by_symbol: dict[str, TradeEvent]
) -> AssetType:
    event = events_by_symbol.get(symbol)
    if event:
        return event.asset_type
    if symbol.endswith("_C") or symbol.endswith("_P"):
        return AssetType.OPTION
    return AssetType.STOCK


def open_mtm_pnl(
    positions: list[Position],
    marks_by_symbol: dict[str, float],
    events: list[TradeEvent],
) -> float:
    """Unrealized P&L on open positions using latest event marks per symbol."""
    events_by_symbol = {e.symbol: e for e in sorted(events, key=lambda ev: ev.timestamp)}
    total = 0.0
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        for symbol, qty in pos.leg_net_qty.items():
            if qty == 0:
                continue
            cost = pos.blended_cost_basis.get(symbol, 0.0)
            mark = marks_by_symbol.get(symbol, cost)
            asset_type = _leg_asset_type(symbol, pos, events_by_symbol)
            if asset_type == AssetType.OPTION:
                pnl = (mark - cost) * qty * OPTION_CONTRACT_MULTIPLIER
            elif asset_type == AssetType.FUTURE:
                mult = futures_multiplier(symbol)
                pnl = (mark - cost) * qty * mult
            else:
                pnl = (mark - cost) * qty
            total += pnl
    return total


def reconstruct_silo_equity_curve(
    events: list[TradeEvent],
    positions: list[Position],
    starting_equity: float,
    silo: Silo,
    marks_provider=None,
) -> pd.DataFrame:
    """Build an equity time series with realized P&L and open-position MTM marks."""
    silo_positions = [p for p in positions if p.silo == silo]
    silo_events = [e for e in events if e.silo == silo]

    if not silo_events:
        return pd.DataFrame(columns=["date", "equity"])

    dates = sorted({e.timestamp.date() for e in silo_events})
    cumulative_pnl = 0.0
    rows = []

    closed_pnl_by_date: dict = {}
    for pos in silo_positions:
        if pos.closed_at:
            d = pos.closed_at.date() if hasattr(pos.closed_at, "date") else pos.closed_at
            closed_pnl_by_date[d] = closed_pnl_by_date.get(d, 0.0) + pos.realized_pnl

    for d in dates:
        cumulative_pnl += closed_pnl_by_date.get(d, 0.0)
        use_live = d == dates[-1]
        if use_live and marks_provider is not None:
            marks, _ = _marks_with_provider(silo_events, silo_positions, marks_provider)
        else:
            marks = {
                sym: price
                for sym, price in _latest_marks_by_symbol(
                    [e for e in silo_events if e.timestamp.date() <= d]
                ).items()
            }
        open_positions = [
            p
            for p in silo_positions
            if p.status == PositionStatus.OPEN
            and p.opened_at
            and (p.opened_at.date() if hasattr(p.opened_at, "date") else p.opened_at) <= d
        ]
        mtm = open_mtm_pnl(open_positions, marks, silo_events)
        open_realized = sum(p.realized_pnl for p in open_positions)
        rows.append({"date": d, "equity": starting_equity + cumulative_pnl + open_realized + mtm})

    return pd.DataFrame(rows)


def build_equity_lookup(
    events: list[TradeEvent],
    closed_entries: list[ClosedTradeEntry],
    starting_equity: dict[Silo, float],
) -> Callable[[Silo, date], float]:
    """Return a function silo + date → cumulative equity for counterfactual sizing.

    Equity at entry uses realized P&L from all closes strictly before that date,
    plus starting equity for the silo.
    """
    pnl_by_silo_date: dict[Silo, dict[date, float]] = {
        Silo.STOCK_OPTIONS: defaultdict(float),
        Silo.FUTURES: defaultdict(float),
    }

    for entry in closed_entries:
        d = entry.closed_at.date()
        pnl_by_silo_date[entry.silo][d] += entry.realized_pnl

    all_dates: dict[Silo, set[date]] = {Silo.STOCK_OPTIONS: set(), Silo.FUTURES: set()}
    for event in events:
        all_dates[event.silo].add(event.timestamp.date())
    for entry in closed_entries:
        all_dates[entry.silo].add(entry.opened_at.date())

    cumulative: dict[Silo, dict[date, float]] = {s: {} for s in Silo}
    for silo in Silo:
        dates = sorted(all_dates[silo])
        running = starting_equity.get(silo, 100_000.0)
        for d in dates:
            cumulative[silo][d] = running
            running += pnl_by_silo_date[silo].get(d, 0.0)

    def lookup(silo: Silo, asof: date) -> float:
        curve = cumulative.get(silo, {})
        if not curve:
            return starting_equity.get(silo, 100_000.0)
        eligible = [d for d in curve if d <= asof]
        if not eligible:
            return starting_equity.get(silo, 100_000.0)
        return curve[max(eligible)]

    return lookup


def equity_metrics_for_silo(
    events: list[TradeEvent],
    positions: list[Position],
    starting_equity: float,
    silo: Silo,
    marks_provider=None,
    *,
    live_equity: float | None = None,
) -> tuple[float, float]:
    """Return (current_equity, peak_equity) including open-position MTM marks."""
    silo_positions = [p for p in positions if p.silo == silo]
    silo_events = [e for e in events if e.silo == silo]

    if not silo_events and not silo_positions:
        current = live_equity if live_equity is not None else starting_equity
        peak = max(starting_equity, current)
        return current, peak

    closed_pnl_by_date: dict[date, float] = {}
    for pos in silo_positions:
        if pos.closed_at and pos.realized_pnl:
            d = pos.closed_at.date() if hasattr(pos.closed_at, "date") else pos.closed_at
            closed_pnl_by_date[d] = closed_pnl_by_date.get(d, 0.0) + pos.realized_pnl

    event_dates = {e.timestamp.date() for e in silo_events}
    all_dates = sorted(event_dates | set(closed_pnl_by_date.keys()))
    if not all_dates:
        current = live_equity if live_equity is not None else starting_equity
        peak = max(starting_equity, current)
        return current, peak

    cumulative_pnl = 0.0
    peak = starting_equity
    current = starting_equity

    open_positions_all = [p for p in silo_positions if p.status == PositionStatus.OPEN]

    for d in all_dates:
        cumulative_pnl += closed_pnl_by_date.get(d, 0.0)
        use_live = d == all_dates[-1]
        if use_live and marks_provider is not None:
            marks, _ = _marks_with_provider(silo_events, silo_positions, marks_provider)
        else:
            marks = {
                sym: price
                for sym, price in _latest_marks_by_symbol(
                    [e for e in silo_events if e.timestamp.date() <= d]
                ).items()
            }
        open_positions = [
            p
            for p in open_positions_all
            if p.opened_at
            and (p.opened_at.date() if hasattr(p.opened_at, "date") else p.opened_at) <= d
        ]
        mtm = open_mtm_pnl(open_positions, marks, silo_events)
        open_realized = sum(p.realized_pnl for p in open_positions)
        current = starting_equity + cumulative_pnl + open_realized + mtm
        peak = max(peak, current)

    if live_equity is not None:
        current = live_equity
        peak = max(peak, live_equity)

    return current, peak


def combined_account_equity_curve(
    events: list[TradeEvent],
    positions: list[Position],
    starting_equity: dict[Silo, float],
    marks_provider=None,
) -> pd.DataFrame:
    """Combined account equity curve across silos for account-level drawdown."""
    frames = []
    for silo in Silo:
        curve = reconstruct_silo_equity_curve(
            events,
            positions,
            starting_equity.get(silo, 100_000.0),
            silo,
            marks_provider=marks_provider,
        )
        if curve.empty:
            continue
        curve = curve.rename(columns={"equity": silo.value})
        frames.append(curve.set_index("date"))

    if not frames:
        total_start = sum(starting_equity.get(s, 100_000.0) for s in Silo)
        return pd.DataFrame([{"date": date.today(), "equity": total_start}])

    merged = pd.concat(frames, axis=1).ffill().fillna(0.0)
    merged["equity"] = merged.sum(axis=1)
    return merged.reset_index()[["date", "equity"]]


def account_equity_metrics(
    events: list[TradeEvent],
    positions: list[Position],
    starting_equity: dict[Silo, float],
    marks_provider=None,
) -> tuple[float, float]:
    """Return (current_account_equity, account_peak_equity) from combined MTM curve."""
    stock_eq, _ = equity_metrics_for_silo(
        events,
        positions,
        starting_equity.get(Silo.STOCK_OPTIONS, 100_000.0),
        Silo.STOCK_OPTIONS,
        marks_provider=marks_provider,
    )
    fut_eq, _ = equity_metrics_for_silo(
        events,
        positions,
        starting_equity.get(Silo.FUTURES, 50_000.0),
        Silo.FUTURES,
        marks_provider=marks_provider,
    )
    current = stock_eq + fut_eq

    curve = combined_account_equity_curve(events, positions, starting_equity, marks_provider)
    if curve.empty:
        return current, current
    peak = float(curve["equity"].max())
    return current, peak
