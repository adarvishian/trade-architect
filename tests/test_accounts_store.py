"""Phase 1 — accounts, balance/holdings snapshot persistence."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_architect.models.entities import Silo
from trading_architect.store.database import Database
from trading_architect.store.repository import (
    BalanceSnapshotRecord,
    HoldingSnapshotRecord,
    Repository,
)


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "accounts_test.db"))


def test_upsert_account_and_list(repo: Repository):
    acct = repo.upsert_account(
        kind="schwab",
        label="schwab-tos",
        silo=Silo.STOCK_OPTIONS,
        institution="Schwab",
    )
    assert acct.id > 0
    assert acct.kind == "schwab"
    assert acct.include_in_deployable is True

    updated = repo.upsert_account(
        kind="schwab",
        label="schwab-tos",
        silo=Silo.STOCK_OPTIONS,
        institution="Charles Schwab",
        include_in_deployable=False,
    )
    assert updated.id == acct.id
    assert updated.institution == "Charles Schwab"
    assert updated.include_in_deployable is False

    listed = repo.list_accounts()
    assert len(listed) == 1
    assert listed[0].label == "schwab-tos"


def test_balance_snapshot_latest_and_history(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="bank-checking", silo=Silo.STOCK_OPTIONS)
    t1 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=t1,
            cash=5_000.0,
            equity_value=5_000.0,
            buying_power=5_000.0,
            source="manual",
        )
    )
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=t2,
            cash=6_000.0,
            equity_value=6_000.0,
            buying_power=6_000.0,
            source="manual",
        )
    )

    latest = repo.latest_balance(acct.id)
    assert latest is not None
    assert latest.equity_value == pytest.approx(6_000.0)
    assert latest.as_of == t2

    all_latest = repo.latest_balances()
    assert all_latest[acct.id].equity_value == pytest.approx(6_000.0)

    history = repo.balance_history(acct.id)
    assert len(history) == 2
    assert history[0].equity_value == pytest.approx(5_000.0)


def test_holdings_snapshots_latest_batch(repo: Repository):
    acct = repo.upsert_account(kind="schwab", label="schwab-tos", silo=Silo.STOCK_OPTIONS)
    as_of = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)

    repo.record_holdings_snapshots(
        acct.id,
        as_of,
        [
            HoldingSnapshotRecord(
                account_id=acct.id,
                as_of=as_of,
                symbol="AAPL",
                asset_type="stock",
                qty=10.0,
                mark=150.0,
                mtm_value=1_500.0,
                cost_basis=140.0,
            ),
            HoldingSnapshotRecord(
                account_id=acct.id,
                as_of=as_of,
                symbol="AAPL_2026-09-18_C_110.0",
                occ_symbol=None,
                asset_type="option",
                qty=2.0,
                mark=5.0,
                mtm_value=1_000.0,
                cost_basis=4.5,
            ),
        ],
    )

    later = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)
    repo.record_holdings_snapshots(
        acct.id,
        later,
        [
            HoldingSnapshotRecord(
                account_id=acct.id,
                as_of=later,
                symbol="AAPL",
                asset_type="stock",
                qty=12.0,
                mark=155.0,
                mtm_value=1_860.0,
                cost_basis=140.0,
            ),
        ],
    )

    latest = repo.latest_holdings(acct.id)
    assert len(latest) == 1
    assert latest[0].qty == pytest.approx(12.0)

    all_latest = repo.latest_holdings()
    assert len(all_latest) == 1


def test_delete_account_cascades_snapshots(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="tradovate-cash", silo=Silo.FUTURES)
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=as_of,
            cash=10_000.0,
            equity_value=10_000.0,
            buying_power=10_000.0,
            source="manual",
        )
    )
    repo.delete_account(acct.id)
    assert repo.list_accounts() == []
    assert repo.latest_balance(acct.id) is None


def test_position_override_upsert(repo: Repository):
    repo.upsert_position_override(
        symbol="AAPL",
        silo=Silo.STOCK_OPTIONS,
        initial_stop=140.0,
        current_stop=145.0,
    )
    ov = repo.get_position_override("AAPL", Silo.STOCK_OPTIONS)
    assert ov is not None
    assert ov.initial_stop == pytest.approx(140.0)
    assert ov.current_stop == pytest.approx(145.0)


def test_size_recommendation_persist(repo: Repository):
    from datetime import datetime, timezone

    ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rec = repo.record_size_recommendation(
        ts=ts,
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        asset_type="stock",
        recommended_qty=200.0,
        dollar_risk=1000.0,
        binding_constraint="fractional_risk",
        inputs_json='{"entry": 100}',
        silo_equity=100_000.0,
    )
    assert rec.id is not None
    listed = repo.list_size_recommendations(limit=1)
    assert len(listed) == 1
    assert listed[0].underlying == "AAPL"
