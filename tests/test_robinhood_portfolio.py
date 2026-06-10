from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from trading_architect.assembly.positions import assemble_positions
from trading_architect.ingestion import robinhood_fetch as rf
from trading_architect.ingestion.robinhood_fetch import stock_account_breakdown
from trading_architect.models.entities import (
    AssetType,
    BrokerAccountBalance,
    ConsolidatedHolding,
    HoldingLeg,
    Side,
    Silo,
    TradeEvent,
)


def test_account_label_for_roth_and_individual():
    assert (
        rf._account_label_for_acct({"type": "ira_roth", "account_number": "1"}) == "robinhood-roth"
    )
    assert (
        rf._account_label_for_acct({"type": "margin", "account_number": "2"})
        == "robinhood-individual"
    )


def test_cash_and_equivalents_from_profile():
    cash, equiv, total = rf._cash_and_equivalents_from_profile(
        {"cash": "1000.00", "portfolio_cash": "1500.50", "uncleared_deposits": "0"}
    )
    assert cash == 1000.0
    assert total == 1500.5
    assert equiv == 500.5


def test_cash_and_equivalents_uses_cash_balances():
    cash, equiv, total = rf._cash_and_equivalents_from_profile(
        {"cash_balances": {"cash": "2000", "total_cash": "2500"}}
    )
    assert cash == 2000.0
    assert total == 2500.0
    assert equiv == 500.0


def test_consolidate_holdings_sum_of_parts():
    legs = [
        HoldingLeg(
            account="robinhood-roth",
            symbol="TSLA",
            underlying="TSLA",
            asset_type=AssetType.STOCK,
            quantity=50,
            average_cost=200,
        ),
        HoldingLeg(
            account="robinhood-individual",
            symbol="TSLA",
            underlying="TSLA",
            asset_type=AssetType.STOCK,
            quantity=100,
            average_cost=180,
        ),
    ]
    consolidated = rf.consolidate_holdings(legs)
    assert len(consolidated) == 1
    row = consolidated[0]
    assert row.total_quantity == 150
    assert row.by_account == {"robinhood-roth": 50, "robinhood-individual": 100}
    assert row.average_cost == pytest.approx(186.66666666666666)


def test_format_account_breakdown():
    text = rf.format_account_breakdown({"robinhood-roth": 50, "robinhood-individual": 100.5})
    assert "roth: 50" in text
    assert "individual: 100.5" in text


def test_fetch_account_balance(monkeypatch):
    rh = MagicMock()
    rh.load_account_profile.return_value = {
        "cash": "1000",
        "portfolio_cash": "1200",
        "buying_power": "5000",
        "type": "ira_roth",
    }
    rh.load_portfolio_profile.return_value = {"equity": "25000", "extended_hours_equity": None}
    monkeypatch.setattr(rf, "_require_robin_stocks", lambda: rh)

    bal = rf.fetch_account_balance("robinhood-roth", account_number="222")
    assert bal.account == "robinhood-roth"
    assert bal.cash_and_equivalents == 1200.0
    assert bal.portfolio_equity == 25000.0


def test_fetch_stock_holdings(monkeypatch):
    rh = MagicMock()
    rh.get_open_stock_positions.return_value = [
        {"quantity": "10", "instrument": "url1", "average_buy_price": "150"},
    ]
    rh.get_symbol_by_url.return_value = "AAPL"
    monkeypatch.setattr(rf, "_require_robin_stocks", lambda: rh)

    legs = rf.fetch_stock_holdings("robinhood-individual", account_number="111")
    assert len(legs) == 1
    assert legs[0].symbol == "AAPL"
    assert legs[0].quantity == 10
    assert legs[0].account == "robinhood-individual"


def test_assemble_positions_tracks_account_breakdown():
    events = [
        TradeEvent(
            broker="robinhood",
            account="robinhood-roth",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            symbol="TSLA",
            underlying="TSLA",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=50,
            price=200,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="a",
        ),
        TradeEvent(
            broker="robinhood",
            account="robinhood-individual",
            timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc),
            symbol="TSLA",
            underlying="TSLA",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=100,
            price=190,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="b",
        ),
    ]
    positions = assemble_positions(events)
    assert len(positions) == 1
    pos = positions[0]
    assert pos.leg_net_qty["TSLA"] == 150
    assert pos.account_leg_qty["TSLA"]["robinhood-roth"] == 50
    assert pos.account_leg_qty["TSLA"]["robinhood-individual"] == 100
    assert stock_account_breakdown(pos) == "individual: 100 | roth: 50"


def test_robinhood_portfolio_snapshot_totals():
    snapshot = rf.RobinhoodPortfolioSnapshot(
        fetched_at=datetime.now(timezone.utc),
        accounts=[
            BrokerAccountBalance(
                account="robinhood-roth",
                cash_and_equivalents=5000,
                portfolio_equity=50000,
            ),
            BrokerAccountBalance(
                account="robinhood-individual",
                cash_and_equivalents=10000,
                portfolio_equity=100000,
            ),
        ],
        holdings=[
            ConsolidatedHolding(
                symbol="TSLA",
                underlying="TSLA",
                asset_type=AssetType.STOCK,
                total_quantity=150,
                by_account={"robinhood-roth": 50, "robinhood-individual": 100},
            )
        ],
    )
    assert snapshot.total_cash_and_equivalents == 15000
    assert snapshot.total_portfolio_equity == 150000
