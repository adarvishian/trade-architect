"""Closed-trade performance summaries (futures / silo-level stats)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from trading_architect.models.entities import Position, PositionStatus, Silo


@dataclass(frozen=True)
class TradePerformanceSummary:
    gross_pnl: float
    total_fees: float
    net_pnl: float
    trade_count: int
    contract_count: int
    win_count: int
    loss_count: int
    win_rate: float
    expectancy: float
    total_profit: float
    total_loss: float
    avg_trade_duration: timedelta | None
    longest_trade_duration: timedelta | None
    avg_win: float | None
    avg_loss: float | None
    largest_win: float | None
    largest_loss: float | None


def summarize_closed_positions(
    positions: list[Position],
    *,
    silo: Silo | None = None,
) -> TradePerformanceSummary | None:
    """Aggregate closed round-trip stats for a silo (matches Tradovate performance layout)."""
    closed = [
        p
        for p in positions
        if p.status == PositionStatus.CLOSED and (silo is None or p.silo == silo)
    ]
    if not closed:
        return None

    pnls = [p.realized_pnl for p in closed]
    wins = [p for p in closed if p.realized_pnl > 0]
    losses = [p for p in closed if p.realized_pnl < 0]
    gross = sum(pnls)
    fees = 0.0  # Tradovate performance CSV does not include per-trade fees
    durations = [
        p.closed_at - p.opened_at
        for p in closed
        if p.opened_at and p.closed_at and p.closed_at >= p.opened_at
    ]
    avg_duration = (
        timedelta(seconds=sum(d.total_seconds() for d in durations) / len(durations))
        if durations
        else None
    )
    longest_duration = max(durations) if durations else None

    return TradePerformanceSummary(
        gross_pnl=gross,
        total_fees=fees,
        net_pnl=gross - fees,
        trade_count=len(closed),
        contract_count=len(closed),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=len(wins) / len(closed) if closed else 0.0,
        expectancy=gross / len(closed) if closed else 0.0,
        total_profit=sum(p.realized_pnl for p in wins),
        total_loss=sum(p.realized_pnl for p in losses),
        avg_trade_duration=avg_duration,
        longest_trade_duration=longest_duration,
        avg_win=(sum(p.realized_pnl for p in wins) / len(wins)) if wins else None,
        avg_loss=(sum(p.realized_pnl for p in losses) / len(losses)) if losses else None,
        largest_win=max((p.realized_pnl for p in wins), default=None),
        largest_loss=min((p.realized_pnl for p in losses), default=None),
    )


def format_duration(delta: timedelta | None) -> str:
    if delta is None:
        return "n/a"
    total_seconds = int(delta.total_seconds())
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}min")
    if seconds or not parts:
        parts.append(f"{seconds}sec")
    return " ".join(parts)
