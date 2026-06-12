"""Multi-account, multi-broker fixture for P5 remediation regression tests."""

from __future__ import annotations

from datetime import date, datetime, timezone

from trading_architect.models.entities import Silo
from trading_architect.services.snapshot_persist import persist_manual_balance
from trading_architect.store.repository import (
    BalanceSnapshotRecord,
    HoldingSnapshotRecord,
    Repository,
)


def seed_multi_account_fixture(
    repo: Repository,
    *,
    deposit_date: date | None = None,
) -> dict[str, int]:
    """2 Schwab + 1 Robinhood + 1 manual bank + 1 manual futures; staggered snapshot dates."""
    deposit_date = deposit_date or date(2026, 3, 15)
    ids: dict[str, int] = {}

    schwab_a = repo.upsert_account(kind="schwab", label="schwab-tos", silo=Silo.STOCK_OPTIONS)
    schwab_b = repo.upsert_account(kind="schwab", label="schwab-ira", silo=Silo.STOCK_OPTIONS)
    rh = repo.upsert_account(kind="robinhood", label="robinhood-roth", silo=Silo.STOCK_OPTIONS)
    bank = repo.upsert_account(
        kind="cash_only",
        label="bank-checking",
        silo=Silo.STOCK_OPTIONS,
        include_in_deployable=True,
    )
    fut = repo.upsert_account(kind="manual", label="tradovate-cash", silo=Silo.FUTURES)

    ids["schwab-tos"] = schwab_a.id
    ids["schwab-ira"] = schwab_b.id
    ids["robinhood-roth"] = rh.id
    ids["bank-checking"] = bank.id
    ids["tradovate-cash"] = fut.id

    settings = repo.load_app_settings()
    settings.schwab_account_hashes = {
        "schwab-tos": "hash-tos",
        "schwab-ira": "hash-ira",
    }
    repo.save_app_settings(settings)

    snapshots = [
        (schwab_a.id, datetime(2026, 3, 1, 16, 0, tzinfo=timezone.utc), 80_000.0),
        (schwab_b.id, datetime(2026, 3, 10, 16, 0, tzinfo=timezone.utc), 40_000.0),
        (rh.id, datetime(2026, 3, 5, 16, 0, tzinfo=timezone.utc), 25_000.0),
        (bank.id, datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc), 50_000.0),
        (fut.id, datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc), 25_000.0),
    ]
    for acct_id, as_of, equity in snapshots:
        repo.record_balance_snapshot(
            BalanceSnapshotRecord(
                account_id=acct_id,
                as_of=as_of,
                cash=equity * 0.1,
                equity_value=equity,
                buying_power=equity * 0.2,
                source="api" if acct_id != bank.id else "manual",
            )
        )

    # Mid-series deposit on schwab-tos (for TWR / deposit detection tests)
    persist_manual_balance(
        repo,
        label="schwab-tos",
        silo=Silo.STOCK_OPTIONS,
        institution="Schwab",
        equity_value=100_000.0,
        cash=10_000.0,
        as_of=datetime.combine(deposit_date, datetime.min.time(), tzinfo=timezone.utc),
    )

    # Robinhood option: per-contract cost basis (500 = $5.00/contract premium)
    as_of_rh = datetime(2026, 3, 5, 16, 0, tzinfo=timezone.utc)
    repo.record_holdings_snapshots(
        rh.id,
        as_of_rh,
        [
            HoldingSnapshotRecord(
                account_id=rh.id,
                as_of=as_of_rh,
                symbol="NVDA_2026-07-18_C_120",
                asset_type="option",
                qty=2.0,
                mark=6.0,
                mtm_value=1_200.0,
                cost_basis=500.0,
            ),
        ],
    )

    # Schwab option: per-share cost basis ($5.00/share)
    as_of_schwab = datetime(2026, 3, 1, 16, 0, tzinfo=timezone.utc)
    repo.record_holdings_snapshots(
        schwab_a.id,
        as_of_schwab,
        [
            HoldingSnapshotRecord(
                account_id=schwab_a.id,
                as_of=as_of_schwab,
                symbol="AAPL_2026-08-15_C_200",
                asset_type="option",
                qty=1.0,
                mark=5.50,
                mtm_value=550.0,
                cost_basis=5.0,
            ),
        ],
    )

    return ids
