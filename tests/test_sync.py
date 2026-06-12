"""Phase 1 — broker sync service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from trading_architect.config.user_settings import AppSettings
from trading_architect.models.entities import (
    AssetType,
    BrokerAccountBalance,
    HoldingLeg,
    SchwabAccountSnapshot,
    Silo,
)
from trading_architect.services.sync import sync_all
from trading_architect.store.database import Database
from trading_architect.store.repository import BalanceSnapshotRecord, Repository


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "sync_test.db"))


def test_manual_account_always_ok(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="bank", silo=Silo.STOCK_OPTIONS)
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=as_of,
            cash=5_000.0,
            equity_value=5_000.0,
            buying_power=5_000.0,
            source="manual",
        )
    )
    results = sync_all(repo, ttl_min=15, rh_session_active=lambda: False)
    manual = next(r for r in results if r.account_label == "bank")
    assert manual.status == "ok"
    assert manual.as_of == as_of


def test_schwab_skips_when_within_ttl(repo: Repository):
    settings = AppSettings(schwab_account_hashes={"schwab-tos": "HASH"})
    repo.save_app_settings(settings)
    acct = repo.upsert_account(kind="schwab", label="schwab-tos", silo=Silo.STOCK_OPTIONS)
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=fresh,
            cash=1_000.0,
            equity_value=50_000.0,
            buying_power=1_000.0,
            source="api",
        )
    )
    with patch("trading_architect.services.sync.schwab_py_available", return_value=True):
        with patch("trading_architect.services.sync.schwab_credentials_configured", return_value=True):
            results = sync_all(repo, ttl_min=15, rh_session_active=lambda: False)
    schwab = next(r for r in results if r.account_label == "schwab-tos")
    assert schwab.status == "ok"
    assert schwab.message == "within TTL"


def test_schwab_sync_persists_snapshot(repo: Repository):
    settings = AppSettings(schwab_account_hashes={"schwab-tos": "HASH"})
    repo.save_app_settings(settings)
    snapshot = SchwabAccountSnapshot(
        fetched_at=datetime.now(timezone.utc),
        accounts=[
            BrokerAccountBalance(
                account="schwab-tos",
                portfolio_equity=99_000.0,
                cash_and_equivalents=5_000.0,
                buying_power=5_000.0,
            )
        ],
        holdings=[],
    )
    legs = [
        HoldingLeg(
            account="schwab-tos",
            symbol="MSFT",
            underlying="MSFT",
            asset_type=AssetType.STOCK,
            quantity=5.0,
            average_cost=400.0,
            market_value=2_000.0,
        )
    ]

    with patch("trading_architect.services.sync.schwab_py_available", return_value=True):
        with patch("trading_architect.services.sync.schwab_credentials_configured", return_value=True):
            with patch(
                "trading_architect.ingestion.schwab_accounts.fetch_portfolio_snapshot_with_legs",
                return_value=(snapshot, legs),
            ):
                results = sync_all(repo, ttl_min=15, rh_session_active=lambda: False, force=True)

    schwab = next(r for r in results if r.account_label == "schwab-tos")
    assert schwab.status == "ok"
    acct = repo.get_account_by_label("schwab-tos")
    bal = repo.latest_balance(acct.id)
    assert bal.equity_value == pytest.approx(99_000.0)
