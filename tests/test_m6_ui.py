"""Tests for drawdown governor and book context (M6)."""

from datetime import datetime

import pytest

from trading_architect.config.user_settings import (
    AppSettings,
    app_settings_from_json,
    app_settings_to_json,
    diff_app_settings,
)
from trading_architect.engines.book_context import build_book_context
from trading_architect.engines.drawdown import (
    DrawdownGovernorState,
    assess_drawdown_state,
    compute_drawdown_pct,
)
from trading_architect.engines.equity import equity_metrics_for_silo
from trading_architect.models.entities import (
    AssetType,
    Direction,
    Position,
    PositionStatus,
    Side,
    Silo,
    TradeEvent,
)
from trading_architect.store.database import Database
from trading_architect.store.repository import Repository


def test_compute_drawdown_pct():
    assert compute_drawdown_pct(90_000, 100_000) == 0.10
    assert compute_drawdown_pct(100_000, 100_000) == 0.0


def test_assess_drawdown_state_ramp():
    normal = assess_drawdown_state(0.05)
    assert normal.state == DrawdownGovernorState.NORMAL
    assert normal.throttle_multiplier == 1.0

    soft = assess_drawdown_state(0.11)
    assert soft.state == DrawdownGovernorState.SOFT_ALERT
    assert soft.throttle_multiplier == 0.85

    hard = assess_drawdown_state(0.21)
    assert hard.state == DrawdownGovernorState.HARD
    assert hard.throttle_multiplier == 0.0


def test_equity_metrics_for_silo_with_closed_pnl(tmp_path):
    events = [
        TradeEvent(
            broker="test",
            account="a",
            timestamp=datetime(2026, 1, 1),
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=10,
            price=100,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="t::1",
        )
    ]
    positions = [
        Position(
            position_id="p1",
            silo=Silo.STOCK_OPTIONS,
            underlying="AAPL",
            direction=Direction.LONG,
            status=PositionStatus.CLOSED,
            component_event_ids=["e1"],
            realized_pnl=5000.0,
            closed_at=datetime(2026, 2, 1),
        )
    ]
    current, peak = equity_metrics_for_silo(events, positions, 100_000.0, Silo.STOCK_OPTIONS)
    assert current == 105_000.0
    assert peak == 105_000.0


def test_open_mtm_moves_equity_before_close():
    from trading_architect.assembly.positions import assemble_positions
    from trading_architect.engines.equity import equity_metrics_for_silo

    events = [
        TradeEvent(
            broker="test",
            account="a",
            timestamp=datetime(2026, 1, 1),
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=100,
            price=100.0,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="t::1",
        ),
    ]
    positions = assemble_positions(events)
    current, _ = equity_metrics_for_silo(events, positions, 100_000.0, Silo.STOCK_OPTIONS)
    assert current == pytest.approx(100_000.0)

    mark_events = events + [
        TradeEvent(
            broker="test",
            account="a",
            timestamp=datetime(2026, 1, 15),
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.SELL,
            quantity=1,
            price=82.0,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="t::mark",
        ),
    ]
    positions = assemble_positions(mark_events)
    current, _ = equity_metrics_for_silo(mark_events, positions, 100_000.0, Silo.STOCK_OPTIONS)
    # 99 shares marked at 82 vs cost 100 → unrealized -1782; realized -18 on 1-share partial close
    assert current == pytest.approx(100_000.0 - 18.0 - 18.0 * 99.0)


def test_build_book_context_effective_drawdown():
    settings = AppSettings(
        starting_equity_stock_options=100_000.0, starting_equity_futures=50_000.0
    )
    positions = [
        Position(
            position_id="p1",
            silo=Silo.STOCK_OPTIONS,
            underlying="AAPL",
            direction=Direction.LONG,
            status=PositionStatus.CLOSED,
            component_event_ids=["e1"],
            realized_pnl=-15_000.0,
            closed_at=datetime(2026, 2, 1),
        )
    ]
    events = [
        TradeEvent(
            broker="test",
            account="a",
            timestamp=datetime(2026, 1, 1),
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=10,
            price=100,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="t::1",
        )
    ]
    book = build_book_context(events, positions, settings)
    assert book.stock_options.equity == 85_000.0
    assert book.stock_options.drawdown_pct == 0.15
    assert book.effective_drawdown_pct >= 0.15
    assert book.effective_governor.state in (
        DrawdownGovernorState.SOFT_ALERT,
        DrawdownGovernorState.THROTTLE,
    )


def test_settings_persistence_roundtrip(tmp_path):
    db = Database(tmp_path / "test.db")
    repo = Repository(db)
    settings = AppSettings(
        starting_equity_stock_options=120_000.0,
        starting_equity_futures=40_000.0,
    )
    repo.save_app_settings(
        settings,
        overrides=diff_app_settings(AppSettings(), settings),
    )
    loaded = repo.load_app_settings()
    assert loaded.starting_equity_stock_options == 120_000.0
    assert loaded.starting_equity_futures == 40_000.0

    raw = app_settings_to_json(loaded)
    restored = app_settings_from_json(raw)
    assert restored.sizing.heat_cap == loaded.sizing.heat_cap

    log = repo.list_override_log()
    assert len(log) >= 2
