"""Phase 3 — surface layer: cash events, book metrics, adherence, dashboard metrics."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from trading_architect.models.entities import Direction, Position, PositionStatus, Silo
from trading_architect.services.adherence import adherence_summary, match_recommendation
from trading_architect.services.book_metrics import heat_utilization
from trading_architect.services.cash_events import detect_cash_jump, trading_equity_adjustment
from trading_architect.services.dashboard_metrics import (
    expiry_runway_premium,
    standard_dollar_risk,
)
from trading_architect.services.snapshot_persist import persist_manual_balance
from trading_architect.store.database import Database
from trading_architect.store.repository import (
    BalanceSnapshotRecord,
    CashEventRecord,
    Repository,
)


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "phase3.db"))


def test_detect_cash_jump_queues_pending(repo: Repository):
    repo.upsert_account(kind="manual", label="bank", silo=Silo.STOCK_OPTIONS)
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    persist_manual_balance(
        repo,
        label="bank",
        silo=Silo.STOCK_OPTIONS,
        institution="Chase",
        equity_value=10_000.0,
        as_of=t0,
    )
    t1 = datetime(2026, 6, 2, tzinfo=timezone.utc)
    persist_manual_balance(
        repo,
        label="bank",
        silo=Silo.STOCK_OPTIONS,
        institution="Chase",
        equity_value=15_000.0,
        as_of=t1,
    )
    pending = repo.list_cash_events(classification="pending")
    assert len(pending) == 1
    assert pending[0].delta == pytest.approx(5_000.0)


def test_detect_cash_jump_skips_small_change(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="bank", silo=Silo.STOCK_OPTIONS)
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=t0,
            cash=10_000.0,
            equity_value=10_000.0,
            buying_power=10_000.0,
            source="manual",
        )
    )
    assert detect_cash_jump(repo, account_id=acct.id, new_cash=10_500.0, as_of=t0) is None


def test_deposit_adjusts_trading_equity(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="bank", silo=Silo.STOCK_OPTIONS)
    repo.record_cash_event(
        CashEventRecord(
            account_id=acct.id,
            detected_at=datetime.now(timezone.utc),
            prior_cash=10_000.0,
            new_cash=20_000.0,
            delta=10_000.0,
            classification="deposit",
        )
    )
    assert trading_equity_adjustment(repo, Silo.STOCK_OPTIONS, 120_000.0) == pytest.approx(110_000.0)


def test_book_metrics_daily_and_heat_utilization(repo: Repository):
    today = date.today()
    repo.record_book_metrics_daily(
        metric_date=today - timedelta(days=30),
        silo=Silo.STOCK_OPTIONS,
        equity=100_000.0,
        open_heat=0.10,
        leverage=1.2,
        theta_day=-50.0,
    )
    repo.record_book_metrics_daily(
        metric_date=today - timedelta(days=1),
        silo=Silo.STOCK_OPTIONS,
        equity=105_000.0,
        open_heat=0.20,
        leverage=1.3,
        theta_day=-60.0,
    )
    util = heat_utilization(repo, silo=Silo.STOCK_OPTIONS, heat_cap=0.5, trailing_days=90)
    assert util == pytest.approx(0.15 / 0.5)


def test_expiry_runway_buckets(repo: Repository):
    pos = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"NVDA_2026-07-15_C_200": 2.0},
        blended_cost_basis={"NVDA_2026-07-15_C_200": 5.0},
    )
    runway = expiry_runway_premium([pos], as_of=date(2026, 6, 1))
    assert runway.under_60 > 0


def test_standard_dollar_risk():
    assert standard_dollar_risk(100_000.0, 0.01, 0.25) == pytest.approx(250.0)


def test_adherence_match_and_summary(repo: Repository, tmp_path):
    from trading_architect.models.entities import AssetType, Side, TradeEvent

    ts = datetime.now(timezone.utc) - timedelta(days=2)
    rec = repo.record_size_recommendation(
        ts=ts,
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        asset_type="stock",
        recommended_qty=100.0,
        dollar_risk=500.0,
        binding_constraint="fractional_risk",
        inputs_json="{}",
        silo_equity=100_000.0,
    )
    fill = TradeEvent(
        event_id="fill-1",
        broker="schwab",
        account="schwab-tos",
        timestamp=ts + timedelta(days=1),
        symbol="AAPL",
        underlying="AAPL",
        asset_type=AssetType.STOCK,
        side=Side.BUY,
        quantity=50.0,
        price=150.0,
        silo=Silo.STOCK_OPTIONS,
        raw_ref="test::1",
    )
    with repo.db.connect() as conn:
        from trading_architect.store.database import event_to_row

        conn.execute(
            """
            INSERT INTO trade_events (
                event_id, natural_key, content_fingerprint, broker, account, timestamp,
                symbol, underlying, asset_type, side, quantity, price, fees,
                option_spec_json, stop_price, silo, raw_ref, epoch_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            event_to_row(fill),
        )

    match = match_recommendation(rec, [fill])
    assert match is not None
    assert match.taken_qty == pytest.approx(50.0)
    assert match.ratio == pytest.approx(0.5)

    summary = adherence_summary(repo, trailing_days=90)
    assert summary.match_count >= 1


def test_review_queue_dismiss(repo: Repository):
    with repo.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO review_queue (source_file, row_index, reason, raw_row_json)
            VALUES ('test.csv', 1, 'bad row', '{}')
            """
        )
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    assert repo.review_queue_count() == 1
    assert repo.dismiss_review_item(str(row_id))
    assert repo.review_queue_count() == 0
