"""Daily book metrics snapshot — powers heat utilization and future analysis."""

from __future__ import annotations

from datetime import datetime, timezone

from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.book_context import BookContext
from trading_architect.models.entities import Silo
from trading_architect.services.dashboard_metrics import options_theta_day
from trading_architect.store.repository import Repository


def maybe_record_daily_metrics(
    repo: Repository,
    book: BookContext,
    *,
    settings: AppSettings | None = None,
    marks_provider=None,
) -> None:
    """Write one row per silo on first sync of each UTC day."""
    today = datetime.now(timezone.utc).date()
    for silo in Silo:
        if repo.has_book_metrics_for_date(today, silo):
            continue
        state = book.stock_options if silo == Silo.STOCK_OPTIONS else book.futures
        theta = 0.0
        if silo == Silo.STOCK_OPTIONS and marks_provider is not None:
            from trading_architect.services.current_state import current_positions

            positions = current_positions(repo, marks_provider)
            theta = options_theta_day(positions, marks_provider)
        repo.record_book_metrics_daily(
            metric_date=today,
            silo=silo,
            equity=state.equity,
            open_heat=state.heat,
            leverage=state.leverage,
            theta_day=theta,
        )


def heat_utilization(
    repo: Repository,
    *,
    silo: Silo = Silo.STOCK_OPTIONS,
    heat_cap: float,
    trailing_days: int = 90,
) -> float | None:
    """Trailing average heat / cap; None when insufficient history."""
    since = datetime.now(timezone.utc).date()
    from datetime import timedelta

    since = since - timedelta(days=trailing_days)
    rows = repo.list_book_metrics_daily(silo=silo, since=since)
    if not rows or heat_cap <= 0:
        return None
    return sum(r.open_heat for r in rows) / len(rows) / heat_cap
