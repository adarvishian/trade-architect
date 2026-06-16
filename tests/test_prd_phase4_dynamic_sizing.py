"""PRD Phase 4 — dynamic sizing correctness (Kelly, parsing, counterfactual delta)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from trading_architect.assembly.entries import ClosedTradeEntry
from trading_architect.config.sizing import SizingConfig
from trading_architect.engines.evaluation import CounterfactualRule, _counterfactual_qty
from trading_architect.engines.sizing import CandidateTrade, SiloExposure, recommend_size
from trading_architect.ingestion.parsing import _pandas_fallback_with_review
from trading_architect.models.entities import (
    AssetType,
    Direction,
    OptionSpec,
    Side,
    Silo,
    TradeEvent,
)


def _closed_stock_pair(
    idx: int,
    *,
    epoch_id: str,
    win: bool = True,
    qty: float = 10.0,
    year: int = 2025,
) -> list[TradeEvent]:
    entry = 100.0
    exit_price = 110.0 if win else 92.0
    stop = 95.0
    open_ts = datetime(2026, 4, 1) + timedelta(days=idx) if year >= 2026 else datetime(year, 1, 1) + timedelta(days=idx)
    close_ts = open_ts + timedelta(days=9)
    return [
        TradeEvent(
            broker="test",
            account="test",
            timestamp=open_ts,
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.BUY,
            quantity=qty,
            price=entry,
            stop_price=stop,
            silo=Silo.STOCK_OPTIONS,
            raw_ref=f"open-{idx}",
            epoch_id=epoch_id,
        ),
        TradeEvent(
            broker="test",
            account="test",
            timestamp=close_ts,
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.SELL,
            quantity=qty,
            price=exit_price,
            silo=Silo.STOCK_OPTIONS,
            raw_ref=f"close-{idx}",
            epoch_id=epoch_id,
        ),
    ]


def _many_closed_trades(
    count: int,
    epoch_id: str,
    *,
    win: bool = True,
    year: int = 2025,
) -> list[TradeEvent]:
    events: list[TradeEvent] = []
    for i in range(count):
        events.extend(_closed_stock_pair(i, epoch_id=epoch_id, win=win, year=year))
    return events


def _stock_candidate() -> CandidateTrade:
    return CandidateTrade(
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        direction=Direction.LONG,
        asset_type=AssetType.STOCK,
        entry_price=100.0,
        stop_price=95.0,
        spot_price=100.0,
    )


@patch("trading_architect.engines.sizing.date")
def test_live_kelly_inactive_below_trade_floor(mock_date):
    mock_date.today.return_value = date(2026, 6, 1)
    events = _many_closed_trades(10, "pre-2026-03-27")
    rec = recommend_size(
        _stock_candidate(),
        SiloExposure(silo_equity=100_000.0),
        events=events,
        config=SizingConfig(min_trades_for_kelly=15),
    )
    assert rec.kelly_target_f is None
    assert any("15" in w for w in rec.warnings)


@patch("trading_architect.engines.sizing.date")
def test_live_kelly_uses_prior_epoch_not_in_sample(mock_date):
    """Prior-epoch winners drive Kelly even when post-epoch book is negative."""
    mock_date.today.return_value = date(2026, 6, 1)
    pre = _many_closed_trades(15, "pre-2026-03-27", win=True, year=2025)
    post = _many_closed_trades(20, "post-2026-03-27", win=False, year=2026)
    rec = recommend_size(
        _stock_candidate(),
        SiloExposure(silo_equity=100_000.0),
        events=pre + post,
        config=SizingConfig(min_trades_for_kelly=15, use_kelly_lower_ci=True),
    )
    assert rec.kelly_target_f is not None
    kelly_layer = next(layer for layer in rec.layers if layer.layer == 3)
    assert "prior-epoch" in kelly_layer.detail.lower()


@patch("trading_architect.engines.sizing.date")
def test_live_kelly_inactive_without_prior_epoch(mock_date):
    mock_date.today.return_value = date(2025, 6, 1)
    events = _many_closed_trades(20, "pre-2026-03-27")
    rec = recommend_size(
        _stock_candidate(),
        SiloExposure(silo_equity=100_000.0),
        events=events,
    )
    assert rec.kelly_target_f is None


def test_malformed_csv_line_routed_to_review_queue(tmp_path):
    content = """Date,Action,Symbol,Quantity,Price
2025-01-01,BUY,AAPL,10,100.00
2025-01-02,SELL,AAPL,10,110.00,extra,fields
2025-01-03,BUY,MSFT,5,200.00
"""
    path = tmp_path / "malformed.csv"
    path.write_text(content, encoding="utf-8")

    df, review = _pandas_fallback_with_review(path)
    assert len(review) == 1
    assert review[0].row_index == 3
    assert "malformed" in review[0].reason.lower()
    assert len(df) == 2



def test_adapter_merges_csv_parse_review_items(monkeypatch, tmp_path):
    import pandas as pd

    from trading_architect.ingestion.schwab import SchwabAdapter
    from trading_architect.models.entities import ReviewQueueItem

    review_item = ReviewQueueItem(
        source_file="x.csv",
        row_index=99,
        reason="malformed CSV line",
        raw_row={"raw_line": "broken"},
    )
    monkeypatch.setattr(
        "trading_architect.ingestion.schwab.read_broker_csv",
        lambda path: (pd.DataFrame(columns=["Action", "Symbol"]), [review_item]),
    )
    _events, review = SchwabAdapter().parse_csv(tmp_path / "x.csv")
    assert len(review) == 1
    assert review[0].row_index == 99


def _option_entry(*, option_delta: float | None) -> ClosedTradeEntry:
    return ClosedTradeEntry(
        entry_id="opt-1",
        silo=Silo.STOCK_OPTIONS,
        underlying="NVDA",
        symbol="NVDA_2026-09-18_C_110.0",
        asset_type=AssetType.OPTION,
        direction=Direction.LONG,
        epoch_id="pre-2026-03-27",
        opened_at=datetime(2025, 6, 1),
        closed_at=datetime(2025, 6, 10),
        quantity=10.0,
        entry_price=1.0,
        exit_price=2.0,
        initial_risk=1_000.0,
        risk_per_unit=100.0,
        realized_pnl=1_000.0,
        realized_r=1.0,
        open_event_id="o1",
        close_event_id="c1",
        risk_basis="premium",
        spot_at_entry=500.0,
        option_delta=option_delta,
    )


def test_counterfactual_uses_supplied_option_delta():
    equity = 100_000.0
    high_delta = _counterfactual_qty(
        _option_entry(option_delta=0.80),
        CounterfactualRule.FRACTIONAL_1PCT,
        equity,
        0.1,
    )
    low_delta = _counterfactual_qty(
        _option_entry(option_delta=0.30),
        CounterfactualRule.FRACTIONAL_1PCT,
        equity,
        0.1,
    )
    default_delta = _counterfactual_qty(
        _option_entry(option_delta=None),
        CounterfactualRule.FRACTIONAL_1PCT,
        equity,
        0.1,
    )
    assert high_delta < low_delta
    assert high_delta != default_delta


def test_closed_entry_carries_option_delta_from_spec():
    events = [
        TradeEvent(
            broker="test",
            account="test",
            timestamp=datetime(2025, 6, 1),
            symbol="NVDA_2026-09-18_C_110.0",
            underlying="NVDA",
            asset_type=AssetType.OPTION,
            side=Side.BUY,
            quantity=1.0,
            price=5.0,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="open",
            option_spec=OptionSpec(
                right="C",
                strike=110.0,
                expiry=date(2026, 9, 18),
                delta_at_entry=0.72,
            ),
        ),
        TradeEvent(
            broker="test",
            account="test",
            timestamp=datetime(2025, 6, 10),
            symbol="NVDA_2026-09-18_C_110.0",
            underlying="NVDA",
            asset_type=AssetType.OPTION,
            side=Side.SELL,
            quantity=1.0,
            price=8.0,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="close",
        ),
    ]
    from trading_architect.assembly.entries import extract_closed_entries

    entries = extract_closed_entries(events)
    assert len(entries) == 1
    assert entries[0].option_delta == pytest.approx(0.72)
