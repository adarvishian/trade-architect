"""Tests for closed trade entry extraction."""

from datetime import datetime

import pytest

from trading_architect.assembly.entries import extract_closed_entries
from trading_architect.models.entities import (
    AssetType,
    Direction,
    OptionSpec,
    Side,
    Silo,
    TradeEvent,
)


def _stock(
    side: Side, qty: float, price: float, ts: datetime, stop: float | None = None
) -> TradeEvent:
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
        raw_ref=f"t::{ts.isoformat()}",
        epoch_id="pre-2026-03-27",
    )


def test_extract_single_stock_round_trip():
    events = [
        _stock(Side.BUY, 10, 100, datetime(2025, 1, 10), stop=95),
        _stock(Side.SELL, 10, 110, datetime(2025, 1, 20)),
    ]
    entries = extract_closed_entries(events)
    assert len(entries) == 1
    e = entries[0]
    assert e.realized_pnl == pytest.approx(100.0)
    assert e.initial_risk == pytest.approx(50.0)  # 10 * (100-95)
    assert e.realized_r == pytest.approx(2.0)


def test_extract_two_tranches_fifo():
    events = [
        _stock(Side.BUY, 10, 100, datetime(2025, 1, 10), stop=95),
        _stock(Side.BUY, 10, 105, datetime(2025, 1, 15), stop=100),
        _stock(Side.SELL, 20, 110, datetime(2025, 1, 25)),
    ]
    entries = extract_closed_entries(events)
    assert len(entries) == 2
    assert entries[0].entry_price == pytest.approx(100.0)
    assert entries[1].entry_price == pytest.approx(105.0)


def test_extract_flip_overshoot_opens_opposite_lot():
    events = [
        _stock(Side.BUY, 10, 100, datetime(2025, 1, 10), stop=95),
        _stock(Side.SELL, 15, 105, datetime(2025, 1, 20)),
        _stock(Side.BUY, 5, 100, datetime(2025, 1, 25)),
    ]
    entries = extract_closed_entries(events)
    assert len(entries) == 2
    assert entries[0].direction == Direction.LONG
    assert entries[0].quantity == pytest.approx(10.0)
    assert entries[1].direction == Direction.SHORT
    assert entries[1].quantity == pytest.approx(5.0)


def test_extract_option_uses_premium_as_r():
    events = [
        TradeEvent(
            broker="test",
            account="test",
            timestamp=datetime(2025, 2, 1),
            symbol="NVDA_C",
            underlying="NVDA",
            asset_type=AssetType.OPTION,
            side=Side.BUY,
            quantity=2,
            price=5.0,
            option_spec=OptionSpec(right="C", strike=900, expiry=datetime(2025, 8, 15).date()),
            silo=Silo.STOCK_OPTIONS,
            raw_ref="t1",
            epoch_id="pre-2026-03-27",
        ),
        TradeEvent(
            broker="test",
            account="test",
            timestamp=datetime(2025, 3, 1),
            symbol="NVDA_C",
            underlying="NVDA",
            asset_type=AssetType.OPTION,
            side=Side.SELL,
            quantity=2,
            price=15.0,
            option_spec=OptionSpec(right="C", strike=900, expiry=datetime(2025, 8, 15).date()),
            silo=Silo.STOCK_OPTIONS,
            raw_ref="t2",
            epoch_id="pre-2026-03-27",
        ),
    ]
    entries = extract_closed_entries(events)
    assert len(entries) == 1
    assert entries[0].initial_risk == pytest.approx(1000.0)  # 2 * 5 * 100
    assert entries[0].realized_r == pytest.approx(2.0)  # 2000 pnl / 1000 R
