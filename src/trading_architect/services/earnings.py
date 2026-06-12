"""Earnings date flags for held underlyings — Phase 4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trading_architect.config.env import schwab_credentials_configured
from trading_architect.ingestion.schwab_auth import schwab_py_available
from trading_architect.models.entities import Position, PositionStatus
from trading_architect.services.benchmark import fetch_earnings_date_schwab
from trading_architect.store.repository import Repository


@dataclass(frozen=True)
class EarningsFlag:
    underlying: str
    earnings_date: date | None
    days_until: int | None
    option_legs_held: int
    source: str
    degraded: bool


def _option_leg_count(position: Position) -> int:
    return sum(1 for sym, qty in position.leg_net_qty.items() if "_" in sym and qty != 0)


def resolve_earnings_date(repo: Repository, underlying: str) -> tuple[date | None, str, bool]:
    """Return (date, source, degraded). Tries Schwab once then manual store."""
    stored = repo.get_earnings_date(underlying)
    if stored and stored.earnings_date:
        return stored.earnings_date, stored.source, stored.source == "manual"

    if schwab_py_available() and schwab_credentials_configured():
        try:
            fetched = fetch_earnings_date_schwab(underlying)
        except Exception:
            fetched = None
        if fetched:
            repo.upsert_earnings_date(underlying, fetched, source="schwab")
            return fetched, "schwab", False

    if stored:
        return stored.earnings_date, stored.source, True
    return None, "unavailable", True


def earnings_flags_for_positions(
    repo: Repository,
    positions: list[Position],
    *,
    as_of: date | None = None,
) -> list[EarningsFlag]:
    """Display-only earnings flags for open underlyings."""
    ref = as_of or date.today()
    underlyings: dict[str, int] = {}
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        underlyings[pos.underlying] = underlyings.get(pos.underlying, 0) + _option_leg_count(pos)

    flags: list[EarningsFlag] = []
    for underlying, opt_count in sorted(underlyings.items()):
        ed, source, degraded = resolve_earnings_date(repo, underlying)
        days = (ed - ref).days if ed else None
        flags.append(
            EarningsFlag(
                underlying=underlying,
                earnings_date=ed,
                days_until=days,
                option_legs_held=opt_count,
                source=source,
                degraded=degraded and ed is None,
            )
        )
    return flags
