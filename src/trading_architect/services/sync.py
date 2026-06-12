"""Broker sync orchestration — TTL-gated refresh with graceful degradation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from trading_architect.config.env import schwab_credentials_configured
from trading_architect.config.user_settings import AppSettings
from trading_architect.ingestion.schwab_auth import SchwabAuthExpired, schwab_py_available
from trading_architect.models.entities import Silo
from trading_architect.services.snapshot_persist import persist_legs_snapshot
from trading_architect.store.repository import AccountRecord, Repository

SyncStatus = Literal["ok", "stale", "auth_required", "error"]


@dataclass(frozen=True)
class SyncResult:
    account_label: str
    status: SyncStatus
    as_of: datetime | None
    message: str


def _snapshot_fresh(as_of: datetime | None, ttl_min: int) -> bool:
    if as_of is None:
        return False
    age = datetime.now(timezone.utc) - as_of.astimezone(timezone.utc)
    return age < timedelta(minutes=ttl_min)


def _age_message(as_of: datetime | None) -> str:
    if as_of is None:
        return "no snapshot yet"
    age = datetime.now(timezone.utc) - as_of.astimezone(timezone.utc)
    minutes = int(age.total_seconds() // 60)
    if minutes < 60:
        return f"last synced {minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"last synced {hours}h ago"
    days = hours // 24
    return f"last synced {days}d ago"


def _sync_schwab_accounts(
    repo: Repository,
    ttl_min: int,
    *,
    force: bool = False,
) -> list[SyncResult]:
    results: list[SyncResult] = []
    settings = repo.load_app_settings()
    labels = sorted(settings.schwab_account_hashes.keys())
    if not labels:
        return results

    if not schwab_py_available() or not schwab_credentials_configured():
        for label in labels:
            acct = repo.get_account_by_label(label)
            as_of = repo.latest_balance(acct.id).as_of if acct else None
            results.append(
                SyncResult(
                    account_label=label,
                    status="stale",
                    as_of=as_of,
                    message="Schwab credentials not configured",
                )
            )
        return results

    stale_labels: list[str] = []
    for label in labels:
        acct = repo.get_account_by_label(label)
        as_of = repo.latest_balance(acct.id).as_of if acct else None
        if force or not _snapshot_fresh(as_of, ttl_min):
            stale_labels.append(label)

    if not stale_labels:
        for label in labels:
            acct = repo.get_account_by_label(label)
            as_of = repo.latest_balance(acct.id).as_of if acct else None
            results.append(
                SyncResult(
                    account_label=label,
                    status="ok",
                    as_of=as_of,
                    message="within TTL",
                )
            )
        return results

    try:
        from trading_architect.ingestion.schwab_accounts import fetch_portfolio_snapshot_with_legs

        snapshot, legs = fetch_portfolio_snapshot_with_legs(
            stale_labels,
            repo=repo,
            persist=False,
        )
        persist_legs_snapshot(
            repo,
            kind="schwab",
            silo=Silo.STOCK_OPTIONS,
            institution="Schwab",
            fetched_at=snapshot.fetched_at,
            accounts=snapshot.accounts,
            legs=legs,
        )
        try:
            from trading_architect.services.benchmark import record_daily_benchmark

            record_daily_benchmark(repo)
        except Exception:
            pass
        for balance in snapshot.accounts:
            results.append(
                SyncResult(
                    account_label=balance.account,
                    status="ok",
                    as_of=snapshot.fetched_at,
                    message="synced",
                )
            )
        for label in labels:
            if label not in {b.account for b in snapshot.accounts}:
                acct = repo.get_account_by_label(label)
                as_of = repo.latest_balance(acct.id).as_of if acct else None
                results.append(
                    SyncResult(
                        account_label=label,
                        status="stale" if as_of else "error",
                        as_of=as_of,
                        message="not returned in fetch",
                    )
                )
    except SchwabAuthExpired as exc:
        for label in labels:
            acct = repo.get_account_by_label(label)
            as_of = repo.latest_balance(acct.id).as_of if acct else None
            results.append(
                SyncResult(
                    account_label=label,
                    status="auth_required",
                    as_of=as_of,
                    message=str(exc),
                )
            )
    except Exception as exc:
        for label in labels:
            acct = repo.get_account_by_label(label)
            as_of = repo.latest_balance(acct.id).as_of if acct else None
            results.append(
                SyncResult(
                    account_label=label,
                    status="error",
                    as_of=as_of,
                    message=str(exc)[:200],
                )
            )
    return results


def _sync_robinhood_accounts(
    repo: Repository,
    ttl_min: int,
    *,
    session_active: bool,
    force: bool = False,
) -> list[SyncResult]:
    results: list[SyncResult] = []
    rh_accounts = [a for a in repo.list_accounts() if a.kind == "robinhood"]
    if not rh_accounts and not session_active:
        return results

    if not session_active:
        for acct in rh_accounts:
            bal = repo.latest_balance(acct.id)
            as_of = bal.as_of if bal else None
            results.append(
                SyncResult(
                    account_label=acct.label,
                    status="stale",
                    as_of=as_of,
                    message=_age_message(as_of),
                )
            )
        return results

    stale = False
    for acct in rh_accounts:
        bal = repo.latest_balance(acct.id)
        as_of = bal.as_of if bal else None
        if force or not _snapshot_fresh(as_of, ttl_min):
            stale = True
            break
    if not stale and rh_accounts:
        for acct in rh_accounts:
            bal = repo.latest_balance(acct.id)
            results.append(
                SyncResult(
                    account_label=acct.label,
                    status="ok",
                    as_of=bal.as_of if bal else None,
                    message="within TTL",
                )
            )
        return results

    try:
        from trading_architect.ingestion.robinhood_fetch import (
            fetch_portfolio_snapshot_with_legs,
            robin_stocks_available,
        )

        if not robin_stocks_available():
            raise ImportError("robin-stocks not installed")

        labels = [a.label for a in rh_accounts] or None
        snapshot, legs = fetch_portfolio_snapshot_with_legs(accounts=labels)
        persist_legs_snapshot(
            repo,
            kind="robinhood",
            silo=Silo.STOCK_OPTIONS,
            institution="Robinhood",
            fetched_at=snapshot.fetched_at,
            accounts=snapshot.accounts,
            legs=legs,
        )
        for balance in snapshot.accounts:
            results.append(
                SyncResult(
                    account_label=balance.account,
                    status="ok",
                    as_of=snapshot.fetched_at,
                    message="synced",
                )
            )
    except Exception as exc:
        for acct in rh_accounts:
            bal = repo.latest_balance(acct.id)
            as_of = bal.as_of if bal else None
            results.append(
                SyncResult(
                    account_label=acct.label,
                    status="error",
                    as_of=as_of,
                    message=str(exc)[:200],
                )
            )
    return results


def _manual_account_results(repo: Repository, accounts: list[AccountRecord]) -> list[SyncResult]:
    results: list[SyncResult] = []
    for acct in accounts:
        if acct.kind != "manual":
            continue
        bal = repo.latest_balance(acct.id)
        as_of = bal.as_of if bal else None
        results.append(
            SyncResult(
                account_label=acct.label,
                status="ok",
                as_of=as_of,
                message=_age_message(as_of) if as_of else "manual entry",
            )
        )
    return results


def sync_all(
    repo: Repository,
    ttl_min: int | None = None,
    *,
    settings: AppSettings | None = None,
    rh_session_active: Callable[[], bool] | None = None,
    force: bool = False,
) -> list[SyncResult]:
    """Refresh broker snapshots when older than TTL; manual accounts report last as_of."""
    settings = settings or repo.load_app_settings()
    ttl = ttl_min if ttl_min is not None else settings.refresh_interval_min
    session_active = rh_session_active() if rh_session_active else False

    results: list[SyncResult] = []
    results.extend(_sync_schwab_accounts(repo, ttl, force=force))
    results.extend(_sync_robinhood_accounts(repo, ttl, session_active=session_active, force=force))
    results.extend(_manual_account_results(repo, repo.list_accounts()))
    return results
