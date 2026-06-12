"""Exit efficiency (MFE/MAE) from holdings mark history — Phase 4."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

from trading_architect.assembly.entries import ClosedTradeEntry, extract_closed_entries
from trading_architect.models.entities import AssetType, Direction
from trading_architect.store.repository import Repository


@dataclass(frozen=True)
class ExitEfficiencyRow:
    underlying: str
    symbol: str
    closed_at: datetime
    exit_r: float | None
    max_r_reached: float | None
    mfe_capture_pct: float | None


@dataclass(frozen=True)
class ExitEfficiencyReport:
    rows: list[ExitEfficiencyRow]
    median_mfe_capture_pct: float | None
    trade_count: int


def _mfe_mae_r(
    entry: ClosedTradeEntry,
    marks: list[tuple[datetime, float]],
) -> tuple[float | None, float | None]:
    if not marks or entry.risk_per_unit <= 0:
        return None, None

    prices = [m for _, m in marks]
    if entry.direction == Direction.LONG:
        mfe = (max(prices) - entry.entry_price) / entry.risk_per_unit
        mae = (entry.entry_price - min(prices)) / entry.risk_per_unit
    else:
        mfe = (entry.entry_price - min(prices)) / entry.risk_per_unit
        mae = (max(prices) - entry.entry_price) / entry.risk_per_unit
    return mfe, mae


def _marks_for_entry(repo: Repository, entry: ClosedTradeEntry) -> list[tuple[datetime, float]]:
    symbols = [entry.symbol]
    if entry.asset_type == AssetType.OPTION and entry.underlying not in symbols:
        symbols.append(entry.underlying)
    for sym in symbols:
        marks = repo.holdings_mark_history(
            sym,
            since=entry.opened_at,
            until=entry.closed_at,
        )
        if marks:
            return marks
    return []


def _row_from_entry(repo: Repository, entry: ClosedTradeEntry) -> ExitEfficiencyRow | None:
    marks = _marks_for_entry(repo, entry)
    if not marks:
        return None
    mfe_r, _ = _mfe_mae_r(entry, marks)
    exit_r = entry.realized_r
    capture = None
    if mfe_r is not None and mfe_r > 0 and exit_r is not None:
        capture = min(exit_r / mfe_r, 1.5)
    return ExitEfficiencyRow(
        underlying=entry.underlying,
        symbol=entry.symbol,
        closed_at=entry.closed_at,
        exit_r=exit_r,
        max_r_reached=mfe_r,
        mfe_capture_pct=capture,
    )


def exit_efficiency_report(
    repo: Repository,
    *,
    limit: int = 20,
) -> ExitEfficiencyReport:
    events = repo.list_events()
    closed = extract_closed_entries(events)
    closed.sort(key=lambda e: e.closed_at, reverse=True)

    rows: list[ExitEfficiencyRow] = []
    captures: list[float] = []

    for entry in closed:
        row = _row_from_entry(repo, entry)
        if row is None:
            continue
        rows.append(row)
        if row.mfe_capture_pct is not None:
            captures.append(row.mfe_capture_pct)
        if len(rows) >= limit:
            break

    median = statistics.median(captures) if captures else None
    return ExitEfficiencyReport(
        rows=rows,
        median_mfe_capture_pct=median,
        trade_count=len(rows),
    )
