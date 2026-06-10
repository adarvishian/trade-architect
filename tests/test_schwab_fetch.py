"""Tests for Schwab transaction fetch normalization."""

import json
from pathlib import Path

import pytest

from trading_architect.ingestion.schwab_fetch import parse_transactions_response
from trading_architect.models.entities import AssetType, Side

FIXTURES = Path(__file__).parent / "fixtures" / "schwab"


def test_parse_transactions_stock_and_option():
    data = json.loads((FIXTURES / "transactions.json").read_text())
    events, review = parse_transactions_response(data, account="schwab-tos")

    assert len(events) == 2
    assert len(review) == 1

    stock = events[0]
    assert stock.asset_type == AssetType.STOCK
    assert stock.underlying == "AAPL"
    assert stock.side == Side.BUY
    assert stock.quantity == 10
    assert stock.price == pytest.approx(195.50)

    opt = events[1]
    assert opt.asset_type == AssetType.OPTION
    assert opt.symbol == "AAPL_2026-09-18_C_110.0"
    assert opt.side == Side.BUY
    assert opt.quantity == 2
    assert opt.option_spec is not None
    assert opt.option_spec.strike == 110.0


def test_malformed_transaction_goes_to_review_queue():
    data = json.loads((FIXTURES / "transactions.json").read_text())
    _, review = parse_transactions_response(data, account="schwab-tos")
    assert review[0].row_index == 2
    assert "Unsupported" in review[0].reason or "Unparseable" in review[0].reason
