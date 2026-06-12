"""Cash jump detection and deposit/withdrawal classification — Phase 3."""

from __future__ import annotations

from datetime import datetime

from trading_architect.store.repository import CashEventRecord, Repository

DEFAULT_MIN_DELTA = 1_000.0
DEFAULT_MIN_PCT = 0.02


def detect_cash_jump(
    repo: Repository,
    *,
    account_id: int,
    new_cash: float,
    as_of: datetime,
    min_delta: float = DEFAULT_MIN_DELTA,
    min_pct: float = DEFAULT_MIN_PCT,
) -> CashEventRecord | None:
    """Queue a pending cash event when cash jumps beyond threshold vs prior snapshot."""
    prior = repo.latest_balance(account_id)
    if prior is None:
        return None

    prior_cash = prior.cash
    if not isinstance(prior_cash, (int, float)) or not isinstance(new_cash, (int, float)):
        return None

    delta = new_cash - prior_cash
    if abs(delta) < min_delta:
        return None
    if prior_cash > 0 and abs(delta) / prior_cash < min_pct:
        return None

    return repo.record_cash_event(
        CashEventRecord(
            account_id=account_id,
            detected_at=as_of,
            prior_cash=prior_cash,
            new_cash=new_cash,
            delta=delta,
            classification="pending",
        )
    )


def classify_cash_event(
    repo: Repository,
    event_id: int,
    classification: str,
) -> CashEventRecord | None:
    return repo.classify_cash_event(event_id, classification)


def pending_cash_events(repo: Repository) -> list[CashEventRecord]:
    return repo.list_cash_events(classification="pending")


def trading_equity_adjustment(
    repo: Repository,
    silo,
    raw_equity: float,
    *,
    as_of: datetime | None = None,
) -> float:
    """Equity net of classified deposits/withdrawals for drawdown governor."""
    from trading_architect.models.entities import Silo

    if not isinstance(silo, Silo):
        silo = Silo(silo)
    return raw_equity - repo.net_cashflow_adjustment_for_silo(silo, as_of=as_of)
