"""Holdings-first current book state — account snapshots drive positions and equity."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from trading_architect.assembly.positions import enrich_positions_with_marks
from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.marks import MarksProvider, default_marks_provider
from trading_architect.ingestion.robinhood_fetch import consolidate_holdings
from trading_architect.models.entities import (
    AssetType,
    Direction,
    HoldingLeg,
    Position,
    PositionStatus,
    Silo,
)
from trading_architect.store.repository import HoldingSnapshotRecord, Repository


def _holding_leg_from_snapshot(
    row: HoldingSnapshotRecord,
    account_label: str,
) -> HoldingLeg:
    return HoldingLeg(
        account=account_label,
        symbol=row.symbol,
        underlying=_underlying_from_symbol(row.symbol),
        asset_type=AssetType(row.asset_type),
        quantity=row.qty,
        average_cost=row.cost_basis,
        market_value=row.mtm_value,
    )


def _underlying_from_symbol(symbol: str) -> str:
    if "_" in symbol:
        return symbol.split("_", 1)[0].upper()
    return symbol.upper()


def holdings_to_positions(
    holdings: list[HoldingSnapshotRecord],
    account_labels: dict[int, str],
) -> list[Position]:
    """Build blended per-underlying positions from latest holdings snapshots."""
    legs = [
        _holding_leg_from_snapshot(row, account_labels[row.account_id])
        for row in holdings
        if row.qty != 0
    ]
    if not legs:
        return []

    consolidated = consolidate_holdings(legs)
    by_underlying: dict[str, list] = defaultdict(list)
    for holding in consolidated:
        by_underlying[holding.underlying].append(holding)

    positions: list[Position] = []
    for underlying, group in sorted(by_underlying.items()):
        leg_net_qty: dict[str, float] = {}
        account_leg_qty: dict[str, dict[str, float]] = {}
        blended_cost: dict[str, float] = {}
        premium_at_risk = 0.0
        silo = Silo.STOCK_OPTIONS

        for holding in group:
            leg_net_qty[holding.symbol] = holding.total_quantity
            blended_cost[holding.symbol] = holding.average_cost
            account_leg_qty[holding.symbol] = dict(holding.by_account)
            if holding.asset_type == AssetType.OPTION and holding.total_quantity > 0:
                premium_at_risk += (
                    abs(holding.average_cost) * abs(holding.total_quantity) * 100
                )

        net_stock = sum(
            q for sym, q in leg_net_qty.items() if "_" not in sym
        )
        direction = Direction.LONG if net_stock >= 0 else Direction.SHORT

        positions.append(
            Position(
                silo=silo,
                underlying=underlying,
                direction=direction,
                status=PositionStatus.OPEN,
                leg_net_qty=leg_net_qty,
                account_leg_qty=account_leg_qty,
                blended_cost_basis=blended_cost,
                premium_at_risk=premium_at_risk,
                opened_at=datetime.now(timezone.utc),
            )
        )

    return positions


def current_positions(
    repo: Repository,
    marks_provider: MarksProvider | None = None,
) -> list[Position]:
    """Consolidated open positions from latest holdings snapshots, enriched with marks."""
    accounts = {a.id: a.label for a in repo.list_accounts()}
    holdings = repo.latest_holdings()
    positions = holdings_to_positions(holdings, accounts)
    if not positions:
        return []

    marks_provider = marks_provider if marks_provider is not None else default_marks_provider()
    enriched = enrich_positions_with_marks(positions, marks_provider, events=[])
    return enriched


def silo_equity_from_snapshots(
    repo: Repository,
    silo: Silo,
    settings: AppSettings,
) -> tuple[float, datetime | None]:
    """Sum latest equity_value for accounts in silo; fallback to starting equity setting."""
    accounts = repo.list_accounts(silo=silo)
    latest = repo.latest_balances()
    total = 0.0
    newest: datetime | None = None
    has_snapshot = False

    for acct in accounts:
        snap = latest.get(acct.id)
        if snap is None:
            continue
        has_snapshot = True
        total += snap.equity_value
        if newest is None or snap.as_of > newest:
            newest = snap.as_of

    if has_snapshot:
        return total, newest

    fallback = settings.starting_equity()[silo]
    return fallback, None


def silo_peak_equity_from_snapshots(repo: Repository, silo: Silo, settings: AppSettings) -> float:
    """High-water mark from balance snapshot history for the silo."""
    accounts = repo.list_accounts(silo=silo)
    if not accounts:
        return settings.starting_equity()[silo]

    peak = settings.starting_equity()[silo]
    for acct in accounts:
        for snap in repo.balance_history(acct.id):
            peak = max(peak, snap.equity_value)
    current, _ = silo_equity_from_snapshots(repo, silo, settings)
    return max(peak, current)


def live_stock_options_equity(
    repo: Repository,
    settings: AppSettings,
) -> float | None:
    """Broker-reported MTM equity when snapshots exist for stock/options silo."""
    equity, as_of = silo_equity_from_snapshots(repo, Silo.STOCK_OPTIONS, settings)
    if as_of is None:
        return None
    return equity


def account_cards(repo: Repository) -> list[dict]:
    """Per-account summary for Accounts page and dashboard."""
    cards: list[dict] = []
    latest_balances = repo.latest_balances()
    for acct in repo.list_accounts():
        bal = latest_balances.get(acct.id)
        cards.append(
            {
                "label": acct.label,
                "kind": acct.kind,
                "institution": acct.institution,
                "silo": acct.silo.value,
                "equity_value": bal.equity_value if bal else None,
                "cash": bal.cash if bal else None,
                "as_of": bal.as_of if bal else None,
                "include_in_deployable": acct.include_in_deployable,
            }
        )
    return cards
