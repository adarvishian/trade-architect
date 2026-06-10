"""Tests for core models and formulas."""

from datetime import datetime

from trading_architect.engines.formulas import (
    dollar_risk_stock,
    fractional_risk_size,
    portfolio_heat,
    r_multiple,
)
from trading_architect.models.entities import AssetType, Side, Silo, TradeEvent


def test_trade_event_natural_key_is_stable():
    event = TradeEvent(
        broker="robinhood",
        account="test",
        timestamp=datetime(2025, 1, 15, 10, 0, 0),
        symbol="AAPL",
        underlying="AAPL",
        asset_type=AssetType.STOCK,
        side=Side.BUY,
        quantity=10,
        price=150.0,
        silo=Silo.STOCK_OPTIONS,
        raw_ref="test.csv::row_1",
    )
    assert "robinhood" in event.natural_key
    assert "150" in event.natural_key


def test_fractional_risk_size():
    size = fractional_risk_size(silo_equity=100_000, f=0.01, risk_per_unit=5.0)
    assert size == 200.0


def test_dollar_risk_stock():
    risk = dollar_risk_stock(entry=100, stop=95, qty=100)
    assert risk == 500.0


def test_r_multiple():
    assert r_multiple(250, 100) == 2.5
    assert r_multiple(100, 0) is None


def test_portfolio_heat():
    assert portfolio_heat(5000, 100_000) == 0.05
