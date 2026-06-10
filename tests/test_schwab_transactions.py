"""Phase 4 — Schwab transaction fetch normalization and idempotent import."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from trading_architect.bootstrap import create_repository, import_schwab_fetch
from trading_architect.ingestion.schwab_auth import SchwabSession
from trading_architect.ingestion.schwab_transactions import (
    fetch_all,
    parse_transactions_response,
)
from trading_architect.models.entities import AssetType, Side

FIXTURES = Path(__file__).parent / "fixtures" / "schwab"
ACCOUNT_HASH = "REDACTED_HASH_VALUE_abc123def456"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)
    yield db_path


@pytest.fixture
def transactions_payload():
    return json.loads((FIXTURES / "transactions.json").read_text())


@pytest.fixture
def mock_session(transactions_payload):
    from enum import Enum

    TxnType = Enum(
        "TransactionType",
        {"TRADE": "TRADE", "RECEIVE_AND_DELIVER": "RECEIVE_AND_DELIVER"},
    )

    class Transactions:
        TransactionType = TxnType

    mock_client = MagicMock()
    mock_client.Transactions = Transactions
    mock_client.get_transactions.return_value = MagicMock(
        status_code=httpx.codes.OK,
        json=lambda: transactions_payload,
    )
    return SchwabSession(mock_client)


def test_parse_transactions_stock_and_option(transactions_payload):
    events, review = parse_transactions_response(transactions_payload, account="schwab-tos")

    assert len(events) == 2
    assert len(review) == 1

    stock = events[0]
    assert stock.asset_type == AssetType.STOCK
    assert stock.underlying == "AAPL"
    assert stock.side == Side.BUY
    assert stock.quantity == 10
    assert stock.price == pytest.approx(195.50)
    assert stock.fees == pytest.approx(0.65)
    assert stock.fill_id == "900001::0"

    opt = events[1]
    assert opt.asset_type == AssetType.OPTION
    assert opt.symbol == "AAPL_2026-09-18_C_110.0"
    assert opt.side == Side.BUY
    assert opt.quantity == 2
    assert opt.option_spec is not None
    assert opt.option_spec.strike == 110.0
    assert opt.fees == pytest.approx(1.30)


def test_malformed_transaction_goes_to_review_queue(transactions_payload):
    _, review = parse_transactions_response(transactions_payload, account="schwab-tos")
    assert review[0].row_index == 2
    assert "Unsupported" in review[0].reason or "Unparseable" in review[0].reason


def test_side_from_instruction_overrides_amount_sign():
    sell_to_close = {
        "activityId": 1,
        "type": "TRADE",
        "instruction": "SELL_TO_CLOSE",
        "time": "2026-05-18T14:30:00+0000",
        "transferItems": [
            {
                "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                "amount": -5,
                "price": 200.0,
            }
        ],
    }
    events, review = parse_transactions_response([sell_to_close], account="schwab-tos")
    assert not review
    assert events[0].side == Side.SELL
    assert events[0].quantity == 5


def test_fetch_all_uses_session(mock_session, transactions_payload):
    events, review = fetch_all(account=ACCOUNT_HASH, client=mock_session)
    assert len(events) == 2
    assert len(review) == 1
    mock_session._client.get_transactions.assert_called_once()


def test_schwab_fetch_reimport_is_idempotent(temp_db, mock_session):
    repo = create_repository()
    r1 = import_schwab_fetch(
        account=ACCOUNT_HASH,
        save_csv=False,
        repo=repo,
        client=mock_session,
    )
    r2 = import_schwab_fetch(
        account=ACCOUNT_HASH,
        save_csv=False,
        repo=repo,
        client=mock_session,
    )
    assert r1.imported == 2
    assert r1.review_queue == 1
    assert r2.imported == 0
    assert r2.skipped_duplicates == 2
