"""Tests for position assembly."""

from datetime import datetime

import pytest

from trading_architect.assembly.positions import assemble_positions, infer_direction
from trading_architect.models.entities import (
    AssetType,
    Direction,
    OptionSpec,
    PositionStatus,
    Side,
    Silo,
    TradeEvent,
)


def _stock_event(side: Side, qty: float, price: float, ts: datetime, stop: float | None = None) -> TradeEvent:
    return TradeEvent(
        broker="test",
        account="test",
        timestamp=ts,
        symbol="AAPL",
        underlying="AAPL",
        asset_type=AssetType.STOCK,
        side=side,
        quantity=qty,
        price=price,
        stop_price=stop,
        silo=Silo.STOCK_OPTIONS,
        raw_ref=f"test::{ts.isoformat()}",
        epoch_id="pre-2026-03-27",
    )


def test_assemble_closed_stock_round_trip():
    events = [
        _stock_event(Side.BUY, 10, 150, datetime(2025, 1, 15), stop=145),
        _stock_event(Side.SELL, 10, 160, datetime(2025, 2, 1)),
    ]
    positions = assemble_positions(events)
    assert len(positions) == 1
    pos = positions[0]
    assert pos.status == PositionStatus.CLOSED
    assert pos.realized_pnl == pytest.approx(100.0)
    assert pos.total_dollar_risk == 0.0
    assert pos.stop_risk == 0.0


def test_open_stock_option_delta_notional():
    events = [
        _stock_event(Side.BUY, 100, 150.0, datetime(2025, 1, 15), stop=145),
        TradeEvent(
            broker="test",
            account="test",
            timestamp=datetime(2025, 1, 16),
            symbol="AAPL_C",
            underlying="AAPL",
            asset_type=AssetType.OPTION,
            side=Side.BUY,
            quantity=5,
            price=3.0,
            option_spec=OptionSpec(
                right="C",
                strike=155,
                expiry=datetime(2025, 6, 20).date(),
                delta_at_entry=0.45,
            ),
            silo=Silo.STOCK_OPTIONS,
            raw_ref="test::opt",
            epoch_id="pre-2026-03-27",
        ),
    ]
    pos = assemble_positions(events)[0]
    assert pos.status == PositionStatus.OPEN
    # 100×150 + 5×0.45×100×150 = 15_000 + 33_750 = 48_750
    assert pos.current_delta_notional == pytest.approx(48_750.0)
    assert pos.total_dollar_risk > 0


def test_long_then_short_creates_two_positions():
    events = [
        _stock_event(Side.BUY, 10, 100, datetime(2025, 1, 10), stop=95),
        _stock_event(Side.SELL, 10, 110, datetime(2025, 1, 20)),
        _stock_event(Side.SELL, 5, 105, datetime(2025, 2, 1), stop=110),
        _stock_event(Side.BUY, 5, 100, datetime(2025, 2, 15)),
    ]
    positions = assemble_positions(events)
    assert len(positions) == 2
    assert positions[0].status == PositionStatus.CLOSED
    assert positions[0].direction == Direction.LONG
    assert positions[1].status == PositionStatus.CLOSED
    assert positions[1].direction == Direction.SHORT


def test_infer_direction_for_calls():
    event = TradeEvent(
        broker="test",
        account="test",
        timestamp=datetime(2025, 1, 1),
        symbol="NVDA_C",
        underlying="NVDA",
        asset_type=AssetType.OPTION,
        side=Side.BUY,
        quantity=1,
        price=10,
        option_spec=OptionSpec(right="C", strike=900, expiry=datetime(2025, 6, 20).date()),
        silo=Silo.STOCK_OPTIONS,
        raw_ref="test",
    )
    assert infer_direction(event) == Direction.LONG


def test_assemble_skips_events_without_ticker():
    events = [
        TradeEvent(
            broker="robinhood",
            account="robinhood-roth",
            timestamp=datetime(2025, 1, 1),
            symbol="",
            underlying="",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=2940,
            price=238.5,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="bad",
        ),
        _stock_event(Side.BUY, 10, 150, datetime(2025, 1, 2), stop=145),
    ]
    positions = assemble_positions(events)
    assert len(positions) == 1
    assert positions[0].underlying == "AAPL"
