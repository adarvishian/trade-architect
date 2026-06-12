"""Size recommendation adherence — match recommendations to subsequent fills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trading_architect.assembly.entries import extract_closed_entries
from trading_architect.models.entities import AssetType, Silo, TradeEvent
from trading_architect.store.repository import Repository, SizeRecommendationRecord

DEFAULT_MATCH_DAYS = 5


@dataclass(frozen=True)
class AdherenceMatch:
    recommendation_id: int
    underlying: str
    silo: Silo
    recommended_qty: float
    taken_qty: float
    ratio: float
    gap_cost: float
    fill_event_id: str | None
    pending: bool = False


@dataclass(frozen=True)
class AdherenceSummary:
    trailing_days: int
    match_count: int
    adherence_pct: float | None
    gap_cost_total: float
    matches: list[AdherenceMatch]


def _first_fill_after(
    events: list[TradeEvent],
    *,
    underlying: str,
    silo: Silo,
    asset_type: str | None,
    since: datetime,
    within_days: int,
) -> TradeEvent | None:
    deadline = since + timedelta(days=within_days)
    for event in events:
        if event.underlying.upper() != underlying.upper():
            continue
        if event.silo != silo:
            continue
        if asset_type and event.asset_type.value != asset_type:
            continue
        ts = event.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if since <= ts <= deadline:
            return event
    return None


def _realized_per_unit_pnl(
    fill: TradeEvent,
    events: list[TradeEvent],
) -> float | None:
    """Per-unit realized P&L from a closed round-trip; None when still open."""
    closed = extract_closed_entries(events)
    for entry in closed:
        if entry.underlying.upper() != fill.underlying.upper():
            continue
        if entry.silo != fill.silo:
            continue
        if entry.symbol != fill.symbol:
            continue
        qty = abs(entry.quantity)
        if qty <= 0:
            continue
        mult = 100 if entry.asset_type == AssetType.OPTION else 1
        return entry.realized_pnl / (qty * mult)
    return None


def match_recommendation(
    rec: SizeRecommendationRecord,
    events: list[TradeEvent],
    *,
    within_days: int = DEFAULT_MATCH_DAYS,
) -> AdherenceMatch | None:
    since = rec.ts
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    fill = _first_fill_after(
        events,
        underlying=rec.underlying,
        silo=rec.silo,
        asset_type=rec.asset_type,
        since=since,
        within_days=within_days,
    )
    if fill is None:
        return None
    taken_qty = abs(fill.quantity)
    ratio = taken_qty / rec.recommended_qty if rec.recommended_qty > 0 else 0.0
    per_unit = _realized_per_unit_pnl(fill, events)
    pending = per_unit is None
    gap_cost = 0.0
    if not pending and ratio < 1.0 and per_unit is not None:
        shortfall = rec.recommended_qty - taken_qty
        gap_cost = max(0.0, shortfall * per_unit) if shortfall > 0 else 0.0
    return AdherenceMatch(
        recommendation_id=rec.id or 0,
        underlying=rec.underlying,
        silo=rec.silo,
        recommended_qty=rec.recommended_qty,
        taken_qty=taken_qty,
        ratio=ratio,
        gap_cost=gap_cost,
        fill_event_id=fill.event_id,
        pending=pending,
    )


def adherence_summary(
    repo: Repository,
    *,
    trailing_days: int = 90,
    within_days: int = DEFAULT_MATCH_DAYS,
) -> AdherenceSummary:
    cutoff = datetime.now(timezone.utc) - timedelta(days=trailing_days)
    recs = [r for r in repo.list_size_recommendations(limit=500) if r.ts >= cutoff]
    events = repo.list_events()
    matches: list[AdherenceMatch] = []
    ratios: list[float] = []
    gap_total = 0.0

    for rec in recs:
        match = match_recommendation(rec, events, within_days=within_days)
        if match is None:
            continue
        matches.append(match)
        ratios.append(min(match.ratio, 1.0))
        if not match.pending and match.ratio < 1.0:
            gap_total += match.gap_cost

    adherence_pct = sum(ratios) / len(ratios) if ratios else None
    return AdherenceSummary(
        trailing_days=trailing_days,
        match_count=len(matches),
        adherence_pct=adherence_pct,
        gap_cost_total=gap_total,
        matches=matches,
    )
