"""Deployable capital and cashflow math — Phase 2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from trading_architect.config.user_settings import AppSettings
from trading_architect.models.entities import Silo
from trading_architect.store.repository import AccountRecord, Repository

BROKERAGE_KINDS = frozenset({"schwab", "robinhood", "tradovate"})


class CapitalBaseMode(str, Enum):
    """How sizing chooses its fractional-risk equity input (PRD §10 D1)."""

    DEPLOYABLE = "deployable"
    SILO_EQUITY = "silo_equity"
    BLEND = "blend"


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


@dataclass(frozen=True)
class SiloDeployableBreakdown:
    """Per-silo deployable pool used as the sizing capital base."""

    silo: Silo
    brokerage_cash: float
    transferable_cash: float
    reserve_held: float
    monthly_net_cashflow: float
    forward_income_included: bool
    deployable_total: float
    as_of: datetime | None = None


@dataclass(frozen=True)
class CapitalBaseResult:
    """Resolved capital base for sizing with transparent arithmetic."""

    amount: float
    mode: CapitalBaseMode
    silo_equity: float
    deployable: SiloDeployableBreakdown | None
    blend_pct: float | None
    detail: str
    warnings: tuple[str, ...] = ()


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


def _eligible_cash_components(
    repo: Repository,
    settings: AppSettings,
    *,
    silo: Silo | None = None,
) -> tuple[float, float, float, float, datetime | None]:
    """Return brokerage cash, non-brokerage cash, reserve, net cashflow, newest as_of."""
    latest = repo.latest_balances()
    accounts = repo.list_accounts(silo=silo) if silo is not None else repo.list_accounts()

    brokerage_cash = 0.0
    non_brokerage_cash = 0.0
    newest: datetime | None = None

    for acct in accounts:
        snap = latest.get(acct.id)
        if snap is None:
            continue
        if newest is None or snap.as_of > newest:
            newest = snap.as_of
        if not acct.include_in_deployable:
            continue
        if _counts_as_brokerage_liquidity(acct, acct.silo):
            brokerage_cash += snap.cash
        elif acct.kind in {"manual", "cash_only"}:
            non_brokerage_cash += snap.cash

    reserve_held = reserve(settings)
    net = monthly_net_cashflow(settings)
    return brokerage_cash, non_brokerage_cash, reserve_held, net, newest


def silo_deployable_capital(
    repo: Repository,
    settings: AppSettings,
    silo: Silo,
    *,
    include_forward_income: bool | None = None,
) -> SiloDeployableBreakdown:
    """Compute deployable pool for one silo (PRD §3.3 R1.2, §10 D1)."""
    forward = (
        settings.include_forward_income
        if include_forward_income is None
        else include_forward_income
    )
    brokerage_cash, non_brokerage_cash, reserve_held, net, newest = _eligible_cash_components(
        repo, settings, silo=silo
    )

    transferable = 0.0
    if silo == Silo.STOCK_OPTIONS:
        transferable = max(0.0, non_brokerage_cash - reserve_held)
    elif non_brokerage_cash > 0:
        transferable = max(0.0, non_brokerage_cash - reserve_held)

    forward_net = net if forward else 0.0
    deployable = max(0.0, brokerage_cash + transferable + forward_net)

    return SiloDeployableBreakdown(
        silo=silo,
        brokerage_cash=brokerage_cash,
        transferable_cash=transferable,
        reserve_held=reserve_held if silo == Silo.STOCK_OPTIONS else 0.0,
        monthly_net_cashflow=net,
        forward_income_included=forward,
        deployable_total=deployable,
        as_of=newest,
    )


def deployable_capital(
    repo: Repository,
    settings: AppSettings,
    *,
    include_forward_income: bool | None = None,
) -> DeployableBreakdown:
    """Compute deployable capital with full breakdown per roadmap §5."""
    latest = repo.latest_balances()
    accounts = repo.list_accounts()

    total_capital = 0.0
    newest: datetime | None = None
    for acct in accounts:
        snap = latest.get(acct.id)
        if snap is None:
            continue
        if acct.kind != "cash_only":
            total_capital += snap.equity_value
        if newest is None or snap.as_of > newest:
            newest = snap.as_of

    forward = (
        settings.include_forward_income
        if include_forward_income is None
        else include_forward_income
    )
    brokerage_cash, non_brokerage_cash, reserve_held, net, _ = _eligible_cash_components(
        repo, settings
    )
    transferable = max(0.0, non_brokerage_cash - reserve_held)
    forward_net = net if forward else 0.0
    deployable = max(0.0, brokerage_cash + transferable + forward_net)

    return DeployableBreakdown(
        total_capital=total_capital,
        brokerage_cash=brokerage_cash,
        transferable_cash=transferable,
        reserve_held=reserve_held,
        monthly_net_cashflow=net,
        deployable_total=deployable,
        as_of=newest,
    )


def _format_deployable_detail(deployable: SiloDeployableBreakdown) -> str:
    parts = [
        f"brokerage cash ${deployable.brokerage_cash:,.0f}",
        f"transferable manual ${deployable.transferable_cash:,.0f}",
    ]
    if deployable.forward_income_included and deployable.monthly_net_cashflow != 0:
        parts.append(f"forward net cashflow ${deployable.monthly_net_cashflow:,.0f}")
    elif not deployable.forward_income_included and deployable.monthly_net_cashflow != 0:
        parts.append(f"forward net cashflow ${deployable.monthly_net_cashflow:,.0f} (excluded)")
    if deployable.reserve_held > 0:
        parts.append(f"reserve held back ${deployable.reserve_held:,.0f}")
    return " + ".join(parts) + f" = **${deployable.deployable_total:,.0f}** deployable"


def resolve_capital_base(
    repo: Repository,
    settings: AppSettings,
    silo: Silo,
    silo_equity: float,
) -> CapitalBaseResult:
    """Resolve the capital base used by the sizing engine (PRD Phase 1)."""
    mode = CapitalBaseMode(settings.capital_base_mode)
    deployable = silo_deployable_capital(repo, settings, silo)
    warnings: list[str] = []

    if mode == CapitalBaseMode.SILO_EQUITY:
        detail = (
            f"Capital base **${silo_equity:,.0f}** — silo equity "
            f"(starting equity + realized P&L + open MTM)."
        )
        return CapitalBaseResult(
            amount=silo_equity,
            mode=mode,
            silo_equity=silo_equity,
            deployable=deployable,
            blend_pct=None,
            detail=detail,
        )

    deployable_amt = deployable.deployable_total
    has_balance_data = deployable.as_of is not None

    if mode == CapitalBaseMode.DEPLOYABLE:
        if not has_balance_data and deployable_amt == 0.0:
            warnings.append("No balance snapshots — falling back to silo equity for capital base.")
            amount = silo_equity
            detail = (
                f"Capital base **${amount:,.0f}** — silo equity fallback "
                f"(no manual/brokerage balances on file)."
            )
        else:
            amount = deployable_amt if deployable_amt > 0 else silo_equity
            if deployable_amt <= 0 and silo_equity > 0:
                warnings.append("Deployable pool is zero — using silo equity instead.")
            detail = (
                f"Capital base **${amount:,.0f}** — deployable pool "
                f"({_format_deployable_detail(deployable).replace('**', '')})."
            )
        return CapitalBaseResult(
            amount=amount,
            mode=mode,
            silo_equity=silo_equity,
            deployable=deployable,
            blend_pct=None,
            detail=detail,
            warnings=tuple(warnings),
        )

    blend_pct = max(0.0, min(1.0, settings.capital_base_blend_pct))
    if not has_balance_data and deployable_amt == 0.0:
        amount = silo_equity
        warnings.append("No balance snapshots — blend uses silo equity only.")
    else:
        dep = deployable_amt if deployable_amt > 0 else silo_equity
        amount = blend_pct * dep + (1.0 - blend_pct) * silo_equity
    detail = (
        f"Capital base **${amount:,.0f}** — {blend_pct:.0%} deployable "
        f"(${deployable_amt:,.0f}) + {1.0 - blend_pct:.0%} silo equity "
        f"(${silo_equity:,.0f})."
    )
    return CapitalBaseResult(
        amount=amount,
        mode=mode,
        silo_equity=silo_equity,
        deployable=deployable,
        blend_pct=blend_pct,
        detail=detail,
        warnings=tuple(warnings),
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
