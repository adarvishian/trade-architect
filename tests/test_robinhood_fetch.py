from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from trading_architect.ingestion import robinhood_fetch as rf
from trading_architect.models.entities import AssetType, OptionSpec, Side, Silo, TradeEvent


def test_resolve_stock_symbol_from_instrument_url():
    rh = MagicMock()
    rh.get_symbol_by_url.return_value = "TSLA"
    order = {"instrument": "https://api.robinhood.com/instruments/abc/"}
    assert rf._resolve_stock_symbol(order, rh) == "TSLA"


def test_resolve_stock_symbol_rejects_unresolved_instrument():
    rh = MagicMock()
    rh.get_symbol_by_url.return_value = None
    order = {"instrument": "https://api.robinhood.com/instruments/abc/"}
    assert rf._resolve_stock_symbol(order, rh) == ""


def test_resolve_option_leg_from_instrument_url():
    rh = MagicMock()
    rh.request_get.return_value = {
        "expiration_date": "2026-07-17",
        "strike_price": "460.0000",
        "type": "call",
        "chain_symbol": "TSLA",
    }
    order = {"chain_symbol": "TSLA"}
    leg = {"option": "https://api.robinhood.com/options/instruments/xyz/"}
    underlying, symbol, spec = rf._resolve_option_leg(order, leg, rh)
    assert underlying == "TSLA"
    assert spec.strike == 460.0
    assert spec.right == "C"
    assert symbol == "TSLA_2026-07-17_C_460.0"


def test_event_to_csv_row_includes_instrument_and_description():
    event = TradeEvent(
        broker="robinhood",
        account="robinhood-roth",
        timestamp=datetime(2026, 5, 18),
        symbol="TSLA_2026-07-17_C_460.0",
        underlying="TSLA",
        asset_type=AssetType.OPTION,
        side=Side.BUY,
        quantity=1,
        price=14.33,
        option_spec=OptionSpec(right="C", strike=460, expiry=datetime(2026, 7, 17).date()),
        silo=Silo.STOCK_OPTIONS,
        raw_ref="test",
    )
    row = rf._event_to_csv_row(event)
    assert row["Instrument"] == "TSLA"
    assert "TSLA" in row["Description"]
    assert "Call" in row["Description"]
    assert row["Trans Code"] == "BTO"


def test_account_number_for_roth(monkeypatch):
    monkeypatch.setattr(
        rf,
        "_load_all_accounts",
        lambda: [
            {"type": "margin", "account_number": "111"},
            {"type": "ira_roth", "account_number": "222"},
        ],
    )
    assert rf._account_number_for_label("robinhood-roth") == "222"


def test_account_number_for_individual(monkeypatch):
    monkeypatch.setattr(
        rf,
        "_load_all_accounts",
        lambda: [{"type": "margin", "account_number": "111"}],
    )
    assert rf._account_number_for_label("robinhood-individual") == "111"


def test_account_number_missing_raises(monkeypatch):
    monkeypatch.setattr(rf, "_load_all_accounts", lambda: [])
    with pytest.raises(ValueError, match="No Robinhood account matched"):
        rf._account_number_for_label("robinhood-roth")


def test_order_timestamp_prefers_last_transaction_at():
    order = {
        "last_transaction_at": "2026-05-18T15:00:00Z",
        "created_at": "2026-05-17T10:00:00Z",
    }
    ts = rf._order_timestamp(order)
    assert ts == datetime(2026, 5, 18, 15, 0, tzinfo=timezone.utc)


def test_order_timestamp_falls_back_to_created_at():
    order = {"created_at": "2026-05-17T10:00:00Z"}
    ts = rf._order_timestamp(order)
    assert ts is not None
    assert ts.year == 2026 and ts.month == 5 and ts.day == 17


def test_fetch_option_orders_uses_created_at(monkeypatch):
    rh = MagicMock()
    rh.get_all_option_orders.return_value = [
        {
            "id": "opt1",
            "state": "filled",
            "created_at": "2026-05-18T12:00:00Z",
            "chain_symbol": "TSLA",
            "average_price": "14.33",
            "quantity": "1",
            "legs": [{"side": "buy", "quantity": "1", "option": "https://example.com/opt/"}],
        }
    ]
    rh.request_get.return_value = {
        "expiration_date": "2026-07-17",
        "strike_price": "460.0000",
        "type": "call",
        "chain_symbol": "TSLA",
    }
    monkeypatch.setattr(rf, "_require_robin_stocks", lambda: rh)
    monkeypatch.setattr(rf, "_account_number_for_label", lambda _label: "111")

    events, review = rf.fetch_option_orders("robinhood-roth")
    assert len(events) == 1
    assert events[0].timestamp.year == 2026
    assert review == []


def test_unresolved_stock_order_goes_to_review_queue(monkeypatch):
    rh = MagicMock()
    rh.get_all_stock_orders.return_value = [
        {
            "id": "stk1",
            "state": "filled",
            "created_at": "2026-05-18T12:00:00Z",
            "side": "buy",
            "quantity": "10",
            "average_price": "100.00",
            "instrument": "https://api.robinhood.com/instruments/bad/",
        }
    ]
    rh.get_symbol_by_url.side_effect = RuntimeError("network error")
    monkeypatch.setattr(rf, "_require_robin_stocks", lambda: rh)
    events, review = rf.fetch_stock_orders("robinhood-roth", account_number="111")
    assert events == []
    assert len(review) == 1
    assert review[0].source_file == "robinhood-api"
    assert "unresolved" in review[0].reason


def test_unresolved_option_leg_goes_to_review_queue(monkeypatch):
    rh = MagicMock()
    rh.get_all_option_orders.return_value = [
        {
            "id": "opt-bad",
            "state": "filled",
            "created_at": "2026-05-18T12:00:00Z",
            "chain_symbol": "TSLA",
            "average_price": "14.33",
            "quantity": "1",
            "legs": [{"side": "buy", "quantity": "1", "option": "https://example.com/opt/"}],
        }
    ]
    rh.request_get.side_effect = ValueError("bad instrument")
    monkeypatch.setattr(rf, "_require_robin_stocks", lambda: rh)
    monkeypatch.setattr(rf, "_account_number_for_label", lambda _label: "111")

    events, review = rf.fetch_option_orders("robinhood-roth")
    assert events == []
    assert len(review) == 1
    assert "option leg unresolved" in review[0].reason
