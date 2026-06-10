"""Tests for Schwab market data normalization."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from trading_architect.ingestion.schwab_rest import (
    fetch_chain_snapshot,
    fetch_quotes,
    parse_option_chain_response,
    parse_quotes_response,
)
from trading_architect.ingestion.schwab_symbols import from_occ as occ_to_synthetic
from trading_architect.ingestion.schwab_symbols import to_occ as synthetic_to_occ

FIXTURES = Path(__file__).parent / "fixtures" / "schwab"


def test_synthetic_to_occ_roundtrip():
    sym = "AAPL_2026-09-18_C_110.0"
    occ = synthetic_to_occ(sym)
    assert occ == "AAPL  260918C00110000"
    assert occ_to_synthetic(occ) == sym


def test_occ_to_synthetic_compact_no_spaces():
    assert occ_to_synthetic("TSLA251219C00400000") == "TSLA_2025-12-19_C_400.0"


def test_parse_option_chain_iv_normalized_to_decimal():
    data = json.loads((FIXTURES / "option_chain_nvda.json").read_text())
    snapshot = parse_option_chain_response(
        data,
        "NVDA",
        min_dte=0,
        max_dte=9999,
        asof=datetime(2026, 5, 21),
    )
    assert snapshot.underlying == "NVDA"
    assert snapshot.spot_price == pytest.approx(950.25)
    assert len(snapshot.contracts) == 3

    call_950 = next(c for c in snapshot.contracts if c.strike == 950.0 and c.right == "C")
    assert call_950.iv == pytest.approx(0.325)
    assert call_950.delta == pytest.approx(0.52)
    assert call_950.mid == pytest.approx(45.5)
    assert call_950.dte == 120


def test_parse_quotes_response():
    data = json.loads((FIXTURES / "quotes.json").read_text())
    parsed = parse_quotes_response(data, asof=datetime(2026, 5, 21))
    assert "AAPL" in parsed
    assert parsed["AAPL"]["price"] == pytest.approx(195.45)
    opt_sym = "AAPL_2026-09-18_C_110.0"
    assert opt_sym in parsed
    assert parsed[opt_sym]["delta"] == pytest.approx(0.42)


class _FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, chain_data, quote_data):
        self._chain = chain_data
        self._quotes = quote_data

    def get_option_chain(self, *args, **kwargs):
        return _FakeResponse(self._chain)

    def get_quotes(self, symbols):
        return _FakeResponse(self._quotes)


def test_fetch_chain_snapshot_with_fake_client():
    chain = json.loads((FIXTURES / "option_chain_nvda.json").read_text())
    client = _FakeClient(chain, {})
    snapshot = fetch_chain_snapshot("NVDA", client=client, min_dte=0, max_dte=9999)
    assert snapshot.spot_price > 0
    assert any(c.gamma is not None for c in snapshot.contracts)


def test_fetch_quotes_with_fake_client():
    quotes = json.loads((FIXTURES / "quotes.json").read_text())
    client = _FakeClient({}, quotes)
    result = fetch_quotes(["AAPL", "AAPL_2026-09-18_C_110.0"], client=client)
    assert result["AAPL"].price == pytest.approx(195.45)
