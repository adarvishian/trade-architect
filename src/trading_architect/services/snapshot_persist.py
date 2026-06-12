"""Persist broker portfolio snapshots to accounts / balance / holdings tables."""

from __future__ import annotations

from datetime import datetime

from trading_architect.ingestion.robinhood_fetch import consolidate_holdings
from trading_architect.models.entities import (
    AssetType,
    BrokerAccountBalance,
    ConsolidatedHolding,
    HoldingLeg,
    Silo,
)
from trading_architect.services.cost_basis import normalize_option_cost_per_share
from trading_architect.store.repository import (
    AccountKind,
    BalanceSnapshotRecord,
    HoldingSnapshotRecord,
    Repository,
)


def _holding_rows_from_consolidated(
    holding: ConsolidatedHolding,
    *,
    account_id: int,
    as_of: datetime,
    broker_kind: AccountKind | None = None,
) -> list[HoldingSnapshotRecord]:
    rows: list[HoldingSnapshotRecord] = []
    total_abs = sum(abs(q) for q in holding.by_account.values()) or abs(holding.total_quantity)
    avg_cost = normalize_option_cost_per_share(
        holding.average_cost,
        holding.asset_type,
        broker_kind=broker_kind,
    )
    for _account_label, qty in holding.by_account.items():
        if qty == 0:
            continue
        share = abs(qty) / total_abs if total_abs else 1.0
        mtm = holding.market_value * share
        mult = 100 if holding.asset_type == AssetType.OPTION else 1
        mark = mtm / (abs(qty) * mult) if qty else None
        rows.append(
            HoldingSnapshotRecord(
                account_id=account_id,
                as_of=as_of,
                symbol=holding.symbol,
                occ_symbol=holding.symbol if holding.asset_type == AssetType.OPTION else None,
                asset_type=holding.asset_type.value,
                qty=qty,
                mark=mark,
                mtm_value=mtm,
                cost_basis=avg_cost,
            )
        )
    return rows


def persist_legs_snapshot(
    repo: Repository,
    *,
    kind: AccountKind,
    silo: Silo,
    institution: str,
    fetched_at: datetime,
    accounts: list[BrokerAccountBalance],
    legs: list[HoldingLeg],
) -> list[str]:
    """Upsert accounts and write balance + holdings snapshots from per-account legs."""
    from trading_architect.services.cash_events import detect_cash_jump

    labels: list[str] = []
    legs_by_account: dict[str, list[HoldingLeg]] = {}
    for leg in legs:
        legs_by_account.setdefault(leg.account, []).append(leg)

    for balance in accounts:
        record = repo.upsert_account(
            kind=kind,
            label=balance.account,
            silo=silo,
            institution=institution,
            preserve_deployable=True,
        )
        labels.append(record.label)
        detect_cash_jump(
            repo,
            account_id=record.id,
            new_cash=balance.cash_and_equivalents,
            as_of=fetched_at,
        )
        repo.record_balance_snapshot(
            BalanceSnapshotRecord(
                account_id=record.id,
                as_of=fetched_at,
                cash=balance.cash_and_equivalents,
                equity_value=balance.portfolio_equity,
                buying_power=balance.buying_power,
                source="api",
            )
        )
        account_legs = legs_by_account.get(balance.account, [])
        consolidated = consolidate_holdings(account_legs)
        holding_rows: list[HoldingSnapshotRecord] = []
        for holding in consolidated:
            holding_rows.extend(
                _holding_rows_from_consolidated(
                    holding,
                    account_id=record.id,
                    as_of=fetched_at,
                    broker_kind=kind,
                )
            )
        repo.record_holdings_snapshots(record.id, fetched_at, holding_rows)

    return labels


def persist_manual_balance(
    repo: Repository,
    *,
    label: str,
    silo: Silo,
    institution: str,
    equity_value: float,
    as_of: datetime,
    cash: float | None = None,
    include_in_deployable: bool = True,
    account_kind: AccountKind = "manual",
) -> int:
    """Record a manual balance update for banks / Tradovate cash accounts."""
    from trading_architect.services.cash_events import detect_cash_jump

    record = repo.upsert_account(
        kind=account_kind,
        label=label,
        silo=silo,
        institution=institution,
        include_in_deployable=include_in_deployable,
    )
    cash_val = equity_value if cash is None else cash
    detect_cash_jump(repo, account_id=record.id, new_cash=cash_val, as_of=as_of)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=record.id,
            as_of=as_of,
            cash=cash_val,
            equity_value=equity_value,
            buying_power=cash_val,
            source="manual",
        )
    )
    return record.id
