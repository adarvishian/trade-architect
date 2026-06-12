"""Phase 1 — Schwab account snapshot tests (no live network)."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.book_context import build_book_context
from trading_architect.ingestion import schwab_accounts as sa
from trading_architect.ingestion.robinhood_fetch import format_holding_label, format_holding_line
from trading_architect.ingestion.schwab_auth import SchwabSession
from trading_architect.models.entities import AssetType

FIXTURES = Path(__file__).parent / "fixtures" / "schwab"


@pytest.fixture
def account_payload():
    return json.loads((FIXTURES / "accounts.json").read_text())


def test_redact_account_number_never_shows_full():
    assert sa.redact_account_number("00001234") == "…1234"
    assert "00001234" not in sa.redact_account_display("schwab-tos", "00001234")


def test_parse_account_balance_uses_liquidation_value(account_payload):
    bal = sa.parse_account_balance(account_payload, label="schwab-tos")
    assert bal.account == "schwab-tos"
    assert bal.portfolio_equity == pytest.approx(125000.5)
    assert bal.buying_power == pytest.approx(85000.0)
    assert bal.cash == pytest.approx(12000.0)
    assert bal.cash_and_equivalents == pytest.approx(15000.0)


def test_format_holding_line_shows_option_strike_and_expiry(account_payload):
    legs = sa.parse_holdings(account_payload, label="schwab-tos")
    from trading_architect.ingestion.robinhood_fetch import consolidate_holdings

    consolidated = consolidate_holdings(legs)
    opt = next(h for h in consolidated if h.asset_type == AssetType.OPTION)
    assert format_holding_label(opt) == "AAPL 2026-09-18 Call $110"
    line = format_holding_line(opt)
    assert "Call $110" in line
    assert "mkt $" in line


def test_parse_holdings_excludes_futures(account_payload):
    legs = sa.parse_holdings(account_payload, label="schwab-tos")
    assert len(legs) == 2
    symbols = {leg.symbol for leg in legs}
    assert "AAPL" in symbols
    assert "AAPL_2026-09-18_C_110.0" in symbols
    assert all(leg.asset_type != AssetType.FUTURE for leg in legs)


def test_fetch_portfolio_snapshot_from_fixture(account_payload):
    from enum import Enum

    class AccountFields(Enum):
        POSITIONS = "positions"

    class Account:
        Fields = AccountFields

    mock_client = MagicMock()
    mock_client.Account = Account
    mock_client.get_account_numbers.return_value = MagicMock(
        status_code=httpx.codes.OK,
        json=lambda: json.loads((FIXTURES / "account_numbers.json").read_text()),
    )
    mock_client.get_account.return_value = MagicMock(
        status_code=httpx.codes.OK,
        json=lambda: account_payload,
    )

    session = SchwabSession(mock_client)
    settings = AppSettings(schwab_account_hashes={"schwab-tos": "REDACTED_HASH_VALUE_abc123def456"})
    repo = MagicMock()
    repo.load_app_settings.return_value = settings
    repo.save_app_settings.side_effect = lambda s, **kw: None

    snapshot = sa.fetch_portfolio_snapshot(["schwab-tos"], client=session, repo=repo)
    assert snapshot.total_portfolio_equity == pytest.approx(125000.5)
    assert len(snapshot.holdings) == 2
    stock = next(
        h for h in snapshot.holdings if h.underlying == "AAPL" and h.asset_type == AssetType.STOCK
    )
    assert stock.total_quantity == pytest.approx(10)


def test_list_accounts_never_exposes_plain_account_numbers():
    mock_client = MagicMock()
    mock_client.get_account_numbers.return_value = MagicMock(
        status_code=httpx.codes.OK,
        json=lambda: json.loads((FIXTURES / "account_numbers.json").read_text()),
    )
    session = SchwabSession(mock_client)
    pairs = sa.list_accounts(session)
    assert pairs
    for _label, hash_val in pairs:
        assert "00001234" not in hash_val


def test_build_book_context_uses_live_snapshot_equity():
    settings = AppSettings(starting_equity_stock_options=100_000.0)
    book = build_book_context(
        [],
        [],
        settings,
        live_stock_options_equity=125_000.5,
        live_stock_options_peak=130_000.0,
    )
    assert book.stock_options.equity == pytest.approx(125_000.5)
