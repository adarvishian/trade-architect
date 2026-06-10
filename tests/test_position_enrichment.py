"""Tests for position enrichment with live marks (H-1)."""

from datetime import datetime, timezone

import pytest

from trading_architect.assembly.positions import assemble_positions, enrich_positions_with_marks
from trading_architect.engines.marks import Mark, StaticMarksProvider
from trading_architect.models.entities import (
    AssetType,
    OptionSpec,
    Side,
    Silo,
    TradeEvent,
)


def _stock_buy(qty, price, ts):
    return TradeEvent(
        broker="test",
        account="test",
        timestamp=ts,
        symbol="AAPL",
        underlying="AAPL",
        asset_type=AssetType.STOCK,
        side=Side.BUY,
        quantity=qty,
        price=price,
        silo=Silo.STOCK_OPTIONS,
        raw_ref="test",
    )


def _option_buy(qty, price, ts):
    return TradeEvent(
        broker="test",
        account="test",
        timestamp=ts,
        symbol="AAPL_2026-09-18_C_110.0",
        underlying="AAPL",
        asset_type=AssetType.OPTION,
        side=Side.BUY,
        quantity=qty,
        price=price,
        option_spec=OptionSpec(
            right="C", strike=110, expiry=datetime(2026, 9, 18).date(), delta_at_entry=0.45
        ),
        silo=Silo.STOCK_OPTIONS,
        raw_ref="test",
    )


def test_enrich_positions_with_marks_sets_delta_notional():
    ts = datetime(2026, 5, 20)
    events = [_stock_buy(100, 150.0, ts), _option_buy(5, 3.0, datetime(2026, 5, 21))]
    positions = assemble_positions(events)
    assert positions[0].current_delta_notional == pytest.approx(48_750.0)

    marks = StaticMarksProvider(
        {
            "AAPL": Mark("AAPL", 160.0, None, ts),
            "AAPL_2026-09-18_C_110.0": Mark("AAPL_2026-09-18_C_110.0", 5.0, 0.55, ts),
        }
    )
    enriched = enrich_positions_with_marks(positions, marks, events)
    pos = enriched[0]
    # 100×160 + 5×0.55×100×160 = 16_000 + 44_000 = 60_000
    assert pos.current_delta_notional == pytest.approx(60_000.0)
    assert pos.current_delta_notional > 0


def test_closed_position_risk_zeroed_on_enrichment():
    ts1 = datetime(2026, 5, 20)
    ts2 = datetime(2026, 5, 25)
    events = [
        _stock_buy(10, 150.0, ts1),
        TradeEvent(
            broker="test",
            account="test",
            timestamp=ts2,
            symbol="AAPL",
            underlying="AAPL",
            asset_type=AssetType.STOCK,
            side=Side.SELL,
            quantity=10,
            price=160.0,
            silo=Silo.STOCK_OPTIONS,
            raw_ref="test2",
        ),
    ]
    positions = assemble_positions(events)
    marks = StaticMarksProvider({"AAPL": Mark("AAPL", 160.0, None, ts2)})
    enriched = enrich_positions_with_marks(positions, marks, events)
    assert enriched[0].total_dollar_risk == 0.0
    assert enriched[0].current_delta_notional == 0.0


def test_schwab_marks_provider_enrichment_nonzero_delta_notional():
    from trading_architect.engines.marks import SchwabMarksProvider
    from trading_architect.ingestion.schwab_streamer import StreamerQuote

    ts = datetime(2026, 5, 20, tzinfo=timezone.utc)
    cache = {
        "AAPL": StreamerQuote(
            symbol="AAPL",
            occ_key="AAPL",
            mark=160.0,
            asof=ts,
        ),
        "AAPL_2026-09-18_C_110.0": StreamerQuote(
            symbol="AAPL_2026-09-18_C_110.0",
            occ_key="AAPL  260918C00110000",
            mark=5.0,
            delta=0.55,
            underlying_price=160.0,
            asof=ts,
            is_option=True,
        ),
    }

    class _FakeStreamer:
        def subscribe(self, equities, options) -> None:
            pass

        def latest(self, symbol: str):
            return cache.get(symbol)

    provider = SchwabMarksProvider(streamer=_FakeStreamer(), use_streamer=True)

    ts_naive = datetime(2026, 5, 20)
    events = [_stock_buy(100, 150.0, ts_naive), _option_buy(5, 3.0, datetime(2026, 5, 21))]
    positions = assemble_positions(events)
    enriched = enrich_positions_with_marks(positions, provider, events)
    assert enriched[0].current_delta_notional == pytest.approx(60_000.0)
