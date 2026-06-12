"""P5 remediation regression tests — production paths, multi-account fixture."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_architect.bootstrap import build_app_book_context
from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.capital import silo_brokerage_liquidity
from trading_architect.engines.marks import Mark, MarksProvider, StaticMarksProvider
from trading_architect.models.entities import (
    AssetType,
    Direction,
    Position,
    PositionStatus,
    Silo,
)
from trading_architect.services.benchmark import parse_fundamental_earnings
from trading_architect.services.cash_events import detect_cash_jump, trading_equity_adjustment
from trading_architect.services.cost_basis import normalize_option_cost_per_share
from trading_architect.services.current_state import (
    current_positions,
    silo_equity_from_snapshots,
    silo_peak_equity_from_snapshots,
)
from trading_architect.services.dashboard_metrics import concentration_clusters, options_theta_day
from trading_architect.services.earnings import earnings_flags_for_positions
from trading_architect.services.position_stops import (
    apply_position_overrides,
    compute_stop_risk,
)
from trading_architect.services.snapshot_persist import persist_manual_balance
from trading_architect.services.sync import sync_all
from trading_architect.store.database import Database
from trading_architect.store.repository import (
    BalanceSnapshotRecord,
    CashEventRecord,
    HoldingSnapshotRecord,
    PositionOverrideRecord,
    Repository,
)

_FIXTURES = Path(__file__).parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))
from multi_account import seed_multi_account_fixture  # noqa: E402


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "p5.db"))


class StaticMarks(StaticMarksProvider):
    def __init__(self, prices: dict[str, float]):
        super().__init__(
            {
                s: Mark(
                    symbol=s,
                    price=p,
                    delta=0.5,
                    asof=datetime.now(timezone.utc),
                )
                for s, p in prices.items()
            }
        )


# --- P1-C1: portfolio heat non-zero from option premium-at-risk ---


def test_p1_c1_holdings_path_portfolio_heat_nonzero(repo: Repository):
    seed_multi_account_fixture(repo)
    settings = AppSettings(starting_equity_stock_options=100_000.0)
    book = build_app_book_context([], [], settings, repo=repo, marks_provider=StaticMarks({}))
    assert book.stock_options.open_dollar_risk > 0
    assert book.stock_options.heat > 0


# --- P1-C2: silo peak from summed series, not per-account max ---


def test_p1_c2_silo_peak_uses_summed_series(repo: Repository):
    a = repo.upsert_account(kind="schwab", label="schwab-tos", silo=Silo.STOCK_OPTIONS)
    b = repo.upsert_account(kind="schwab", label="schwab-ira", silo=Silo.STOCK_OPTIONS)
    settings = AppSettings(
        starting_equity_stock_options=100_000.0,
        schwab_account_hashes={"schwab-tos": "h1", "schwab-ira": "h2"},
    )
    repo.save_app_settings(settings)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=a.id,
            as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
            cash=0,
            equity_value=80_000.0,
            buying_power=0,
            source="api",
        )
    )
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=b.id,
            as_of=datetime(2026, 3, 10, tzinfo=timezone.utc),
            cash=0,
            equity_value=50_000.0,
            buying_power=0,
            source="api",
        )
    )
    peak = silo_peak_equity_from_snapshots(repo, Silo.STOCK_OPTIONS, settings)
    assert peak == pytest.approx(130_000.0)


# --- P1-C3: cash-only bank excluded from silo equity ---


def test_p1_c3_cash_only_excluded_from_silo_equity(repo: Repository):
    seed_multi_account_fixture(repo)
    settings = AppSettings(starting_equity_stock_options=100_000.0)
    equity, _ = silo_equity_from_snapshots(repo, Silo.STOCK_OPTIONS, settings)
    assert equity == pytest.approx(100_000.0 + 40_000.0 + 25_000.0)


# --- P1-C4: Robinhood per-contract vs Schwab per-share cost basis ---


def test_p1_c4_option_cost_basis_broker_units():
    assert normalize_option_cost_per_share(500.0, AssetType.OPTION, broker_kind="robinhood") == pytest.approx(5.0)
    assert normalize_option_cost_per_share(5.0, AssetType.OPTION, broker_kind="schwab") == pytest.approx(5.0)


def test_p1_c4_rh_premium_at_risk_not_100x(repo: Repository):
    seed_multi_account_fixture(repo)
    positions = current_positions(repo, StaticMarks({}))
    nvda = next(p for p in positions if p.underlying == "NVDA")
    assert nvda.premium_at_risk == pytest.approx(1_000.0)


# --- P1-H5: empty holdings batch clears stale positions ---


def test_p1_h5_empty_holdings_clears_stale(repo: Repository):
    acct = repo.upsert_account(kind="schwab", label="schwab-tos", silo=Silo.STOCK_OPTIONS)
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
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
            )
        ],
    )
    repo.record_holdings_snapshots(acct.id, datetime(2026, 6, 2, tzinfo=timezone.utc), [])
    assert current_positions(repo) == []


# --- P1-H6: orphan Schwab account excluded from equity ---


def test_p1_h6_orphan_schwab_excluded(repo: Repository, monkeypatch):
    orphan = repo.upsert_account(kind="schwab", label="schwab-old", silo=Silo.STOCK_OPTIONS)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=orphan.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=0,
            equity_value=999_999.0,
            buying_power=0,
            source="api",
        )
    )
    active = repo.upsert_account(kind="schwab", label="schwab-tos", silo=Silo.STOCK_OPTIONS)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=active.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=0,
            equity_value=50_000.0,
            buying_power=0,
            source="api",
        )
    )
    settings = AppSettings(schwab_account_hashes={"schwab-tos": "hash1"})
    repo.save_app_settings(settings)
    equity, _ = silo_equity_from_snapshots(repo, Silo.STOCK_OPTIONS, settings)
    assert equity == pytest.approx(50_000.0)


# --- P1-H7: sync preserves include_in_deployable ---


def test_p1_h7_preserve_include_in_deployable(repo: Repository):
    repo.upsert_account(
        kind="schwab",
        label="schwab-tos",
        silo=Silo.STOCK_OPTIONS,
        include_in_deployable=False,
    )
    updated = repo.upsert_account(
        kind="schwab",
        label="schwab-tos",
        silo=Silo.STOCK_OPTIONS,
        include_in_deployable=True,
        preserve_deployable=True,
    )
    assert updated.include_in_deployable is False


# --- P1-H8 / P3-H7: stale sync status from snapshot age ---


def test_p1_h8_stale_sync_when_snapshot_old(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="bank", silo=Silo.STOCK_OPTIONS)
    old = datetime.now(timezone.utc) - timedelta(days=5)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=old,
            cash=10_000.0,
            equity_value=10_000.0,
            buying_power=10_000.0,
            source="manual",
        )
    )
    results = sync_all(repo, ttl_min=15)
    manual = next(r for r in results if r.account_label == "bank")
    assert manual.status == "stale"


# --- P1-H9: bootstrap merges futures ledger with holdings ---


def test_p1_h9_futures_ledger_retained_with_holdings(repo: Repository):
    seed_multi_account_fixture(repo)
    fut_pos = Position(
        silo=Silo.FUTURES,
        underlying="ES",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"ES": 1.0},
        blended_cost_basis={"ES": 5000.0},
        current_delta_notional=25_000.0,
    )
    settings = AppSettings()
    ctx = build_app_book_context([], [fut_pos], settings, repo=repo, marks_provider=StaticMarks({}))
    assert ctx.futures.open_position_count == 1
    assert len(current_positions(repo, StaticMarks({}))) >= 1


# --- P2-C1: futures manual cash counts ---


def test_p2_c1_futures_manual_liquidity(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="tradovate", silo=Silo.FUTURES)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=25_000.0,
            equity_value=25_000.0,
            buying_power=25_000.0,
            source="manual",
        )
    )
    cash, _ = silo_brokerage_liquidity(repo, Silo.FUTURES)
    assert cash == pytest.approx(25_000.0)


# --- P2-C2 / P3-H5: options open-R uses premium basis ---


def test_p2_c2_option_open_r_sane(repo: Repository):
    pos = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"NVDA_2026-07-18_C_120": 2.0},
        blended_cost_basis={"NVDA_2026-07-18_C_120": 5.0},
        premium_at_risk=1_000.0,
        current_delta_notional=12_000.0,
    )
    marks = StaticMarks({"NVDA_2026-07-18_C_120": 6.0})
    enriched = apply_position_overrides([pos], repo, marks_provider=marks)
    open_r = enriched[0].open_r
    assert open_r is not None
    assert abs(open_r) < 5.0


# --- P2-C3: option stop does not double-count premium ---


def test_p2_c3_option_stop_no_double_heat(repo: Repository):
    pos = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"NVDA_2026-07-18_C_120": 2.0},
        blended_cost_basis={"NVDA_2026-07-18_C_120": 5.0},
        premium_at_risk=1_000.0,
    )
    override = PositionOverrideRecord(
        symbol="NVDA",
        silo=Silo.STOCK_OPTIONS,
        initial_stop=100.0,
        current_stop=100.0,
    )
    assert compute_stop_risk(pos, override) == 0.0
    enriched = apply_position_overrides([pos], repo)
    assert enriched[0].total_dollar_risk == pytest.approx(1_000.0)


# --- P2-H6: stop can be cleared ---


def test_p2_h6_clear_position_stop(repo: Repository):
    repo.upsert_position_override(
        symbol="AAPL",
        silo=Silo.STOCK_OPTIONS,
        initial_stop=140.0,
        current_stop=140.0,
    )
    repo.upsert_position_override(
        symbol="AAPL",
        silo=Silo.STOCK_OPTIONS,
        initial_stop=None,
        current_stop=None,
    )
    assert repo.get_position_override("AAPL", Silo.STOCK_OPTIONS).initial_stop is None


# --- P3-C1: deposit detection via persist_manual_balance ---


def test_p3_c1_deposit_detection_via_persist(repo: Repository):
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
    persist_manual_balance(
        repo,
        label="bank",
        silo=Silo.STOCK_OPTIONS,
        institution="Chase",
        equity_value=25_000.0,
        as_of=datetime(2026, 6, 2, tzinfo=timezone.utc),
    )
    pending = repo.list_cash_events(classification="pending")
    assert len(pending) == 1
    assert pending[0].delta == pytest.approx(15_000.0)


# --- P3-C2: deposit adjustment as-of aware ---


def test_p3_c2_deposit_adjustment_as_of(repo: Repository):
    acct = repo.upsert_account(kind="manual", label="bank", silo=Silo.STOCK_OPTIONS)
    before = datetime(2026, 3, 1, tzinfo=timezone.utc)
    deposit_day = datetime(2026, 3, 15, tzinfo=timezone.utc)
    repo.record_cash_event(
        CashEventRecord(
            account_id=acct.id,
            detected_at=deposit_day,
            prior_cash=80_000.0,
            new_cash=100_000.0,
            delta=20_000.0,
            classification="deposit",
        )
    )
    adj_before = trading_equity_adjustment(repo, Silo.STOCK_OPTIONS, 120_000.0, as_of=before)
    adj_after = trading_equity_adjustment(repo, Silo.STOCK_OPTIONS, 120_000.0, as_of=deposit_day)
    assert adj_before == pytest.approx(120_000.0)
    assert adj_after == pytest.approx(100_000.0)


# --- P3-C3: display equity is raw, not deposit-adjusted ---


def test_p3_c3_display_equity_not_deposit_adjusted(repo: Repository):
    acct = repo.upsert_account(kind="schwab", label="schwab-tos", silo=Silo.STOCK_OPTIONS)
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cash=0,
            equity_value=120_000.0,
            buying_power=0,
            source="api",
        )
    )
    repo.record_cash_event(
        CashEventRecord(
            account_id=acct.id,
            detected_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            prior_cash=100_000.0,
            new_cash=120_000.0,
            delta=20_000.0,
            classification="deposit",
        )
    )
    settings = AppSettings(
        starting_equity_stock_options=100_000.0,
        schwab_account_hashes={"schwab-tos": "hash1"},
    )
    repo.save_app_settings(settings)
    book = build_app_book_context([], [], settings, repo=repo)
    assert book.stock_options.equity == pytest.approx(120_000.0)


# --- P3-H6: theta None when marks missing ---


def test_p3_h6_theta_none_without_marks():
    pos = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"NVDA_2026-07-18_C_120": 1.0},
        blended_cost_basis={"NVDA_2026-07-18_C_120": 5.0},
    )

    class EmptyMarks(MarksProvider):
        def marks_for(self, symbols):
            return {}

    assert options_theta_day([pos], EmptyMarks()) is None


# --- P3-H9: second cash jump not suppressed ---


def test_p3_h9_second_cash_jump_recorded(repo: Repository):
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
    detect_cash_jump(repo, account_id=acct.id, new_cash=25_000.0, as_of=datetime(2026, 6, 2, tzinfo=timezone.utc))
    repo.record_balance_snapshot(
        BalanceSnapshotRecord(
            account_id=acct.id,
            as_of=datetime(2026, 6, 2, tzinfo=timezone.utc),
            cash=25_000.0,
            equity_value=25_000.0,
            buying_power=25_000.0,
            source="manual",
        )
    )
    detect_cash_jump(repo, account_id=acct.id, new_cash=40_000.0, as_of=datetime(2026, 6, 3, tzinfo=timezone.utc))
    pending = repo.list_cash_events(classification="pending")
    assert len(pending) == 2


# --- P4-C1: multi-account equity series forward-fill ---


def test_p4_c1_daily_equity_forward_fill(repo: Repository):
    seed_multi_account_fixture(repo)
    series = repo.daily_equity_series(silo=Silo.STOCK_OPTIONS)
    by_date = dict(series)
    assert by_date[date(2026, 3, 1)] == pytest.approx(80_000.0)
    assert by_date[date(2026, 3, 5)] == pytest.approx(105_000.0)
    assert by_date[date(2026, 3, 10)] == pytest.approx(145_000.0)
    assert by_date[date(2026, 3, 15)] == pytest.approx(165_000.0)


# --- P4-C2: no dividend fallback for earnings ---


def test_p4_c2_earnings_ignores_dividend_dates():
    data = {
        "NVDA": {
            "fundamental": {
                "nextDividendExDate": "2026-07-01",
            }
        }
    }
    assert parse_fundamental_earnings(data, "NVDA") is None


# --- P4-H8: manual earnings suffix reachable ---


def test_p4_h8_manual_earnings_not_degraded_when_dated(repo: Repository):
    repo.upsert_earnings_date("NVDA", date.today() + timedelta(days=4), source="manual")
    pos = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"NVDA_2026-09-18_C_110.0": 1.0},
        blended_cost_basis={"NVDA_2026-09-18_C_110.0": 5.0},
    )
    flags = earnings_flags_for_positions(repo, [pos])
    assert flags[0].source == "manual"
    assert flags[0].earnings_date is not None


# --- P4-H10: cluster concentration uses abs notional ---


def test_p4_h10_cluster_uses_abs_notional(repo: Repository):
    long_p = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        current_delta_notional=80_000.0,
    )
    short_p = Position(
        silo=Silo.STOCK_OPTIONS,
        underlying="AMD",
        direction=Direction.SHORT,
        status=PositionStatus.OPEN,
        current_delta_notional=-20_000.0,
    )
    clusters = concentration_clusters([long_p, short_p], repo)
    total = sum(n for _, n in clusters)
    assert total == pytest.approx(100_000.0)
