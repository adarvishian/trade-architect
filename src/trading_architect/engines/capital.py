"""Deployable capital and cashflow math — Phase 2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_architect.config.user_settings import AppSettings
from trading_architect.models.entities import Silo
from trading_architect.store.repository import AccountRecord, Repository

BROKERAGE_KINDS = frozenset({"schwab", "robinhood", "tradovate"})


def monthly_net_cashflow(settings: AppSettings) -> float:
    """Income after tax minus monthly expenses."""
    return settings.monthly_income_after_tax - settings.monthly_expenses


def reserve(settings: AppSettings) -> float:
    """Cash held back to cover living expenses."""
    return settings.cash_reserve_months * settings.monthly_expenses


@dataclass(frozen=True)
class DeployableBreakdown:
    total_capital: float
    brokerage_cash: float
    transferable_cash: float
    reserve_held: float
    monthly_net_cashflow: float
    deployable_total: float
    as_of: datetime | None = None


def _counts_as_brokerage_liquidity(acct: AccountRecord, silo: Silo) -> bool:
    if acct.silo != silo:
        return False
    if acct.kind in BROKERAGE_KINDS:
        return True
    return acct.kind == "manual" and silo == Silo.FUTURES


def silo_brokerage_liquidity(repo: Repository, silo: Silo) -> tuple[float | None, float | None]:
    """Sum brokerage liquidity; None when no snapshots exist (missing data ≠ $0)."""
    latest = repo.latest_balances()
    cash = 0.0
    buying_power = 0.0
    has_data = False
    for acct in repo.list_accounts(silo=silo):
        if not _counts_as_brokerage_liquidity(acct, silo):
            continue
        snap = latest.get(acct.id)
        if snap is None:
            continue
        has_data = True
        cash += snap.cash
        buying_power += snap.buying_power
    if not has_data:
        return None, None
    liquidity = max(cash, buying_power)
    return liquidity, buying_power


def deployable_capital(repo: Repository, settings: AppSettings) -> DeployableBreakdown:
    """Compute deployable capital with full breakdown per roadmap §5."""
    latest = repo.latest_balances()
    accounts = repo.list_accounts()

    total_capital = 0.0
    brokerage_cash = 0.0
    non_brokerage_cash = 0.0
    newest: datetime | None = None

    for acct in accounts:
        snap = latest.get(acct.id)
        if snap is None:
            continue
        if acct.kind != "cash_only":
            total_capital += snap.equity_value
        if newest is None or snap.as_of > newest:
            newest = snap.as_of
        if not acct.include_in_deployable:
            continue
        if acct.kind in BROKERAGE_KINDS:
            brokerage_cash += snap.cash
        elif acct.kind in {"manual", "cash_only"}:
            non_brokerage_cash += snap.cash

    reserve_held = reserve(settings)
    transferable = max(0.0, non_brokerage_cash - reserve_held)
    net = monthly_net_cashflow(settings)
    deployable = brokerage_cash + transferable + net

    return DeployableBreakdown(
        total_capital=total_capital,
        brokerage_cash=brokerage_cash,
        transferable_cash=transferable,
        reserve_held=reserve_held,
        monthly_net_cashflow=net,
        deployable_total=deployable,
        as_of=newest,
    )


def project_equity(
    current_equity: float,
    monthly_contribution: float,
    annual_return_assumption: float,
    months: int,
) -> list[float]:
    """Project equity forward month-by-month (index 0 = current)."""
    if months < 0:
        raise ValueError("months must be >= 0")
    monthly_rate = annual_return_assumption / 12.0
    values = [current_equity]
    for _ in range(months):
        values.append(values[-1] * (1.0 + monthly_rate) + monthly_contribution)
    return values
