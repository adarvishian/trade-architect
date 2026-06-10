"""Tests for Schwab REST chain and quote normalization (PRD §16.4 Phase 3)."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from trading_architect.engines.options_selector import (
    OptionDirection,
    OptionSelectorInput,
    _contract_greeks,
    rank_contracts,
)
from trading_architect.engines.sizing import SiloExposure
from trading_architect.ingestion.schwab_rest import (
    fetch_chain_snapshot,
    fetch_quotes,
    parse_option_chain_response,
    parse_quotes_response,
)
from trading_architect.ingestion.schwab_symbols import from_occ, to_occ
from trading_architect.models.entities import ChainContract, ChainSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "schwab"


def test_synthetic_to_occ_roundtrip():
    sym = "AAPL_2026-09-18_C_110.0"
    occ = to_occ(sym)
    assert occ == "AAPL  260918C00110000"
    assert from_occ(occ) == sym


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


def test_fetch_quotes_returns_mark_objects():
    quotes = json.loads((FIXTURES / "quotes.json").read_text())
    client = _FakeClient({}, quotes)
    result = fetch_quotes(["AAPL", "AAPL_2026-09-18_C_110.0"], client=client)
    assert result["AAPL"].price == pytest.approx(195.45)
    assert result["AAPL"].symbol == "AAPL"
    opt = result["AAPL_2026-09-18_C_110.0"]
    assert opt.delta == pytest.approx(0.42)


def test_contract_greeks_fills_missing_delta_only():
    snap = ChainSnapshot(
        underlying="NVDA",
        asof_timestamp=datetime(2026, 5, 21),
        spot_price=950.0,
        contracts=[],
    )
    contract = ChainContract(
        strike=950.0,
        expiry=datetime(2026, 9, 18).date(),
        dte=120,
        mid=45.5,
        iv=0.325,
        delta=None,
        theta=-0.35,
        vega=1.2,
        gamma=0.004,
        right="C",
    )
    greeks = _contract_greeks(contract, snap, 45.5)
    assert greeks["theta"] == pytest.approx(-0.35)
    assert greeks["vega"] == pytest.approx(1.2)
    assert greeks["delta"] is not None
    assert 0 < greeks["delta"] < 1


def test_rank_contracts_with_schwab_chain_fixture():
    data = json.loads((FIXTURES / "option_chain_nvda.json").read_text())
    snapshot = parse_option_chain_response(
        data,
        "NVDA",
        min_dte=90,
        max_dte=400,
        asof=datetime(2026, 5, 21),
    )
    result = rank_contracts(
        snapshot,
        OptionSelectorInput(
            underlying="NVDA",
            direction=OptionDirection.LONG_CALL,
            target_price=1000.0,
            expected_hold_days=60,
        ),
        SiloExposure(silo_equity=100_000.0),
        top_n=3,
    )
    assert result.ranked
    assert len(result.ranked) <= 3
    for item in result.ranked:
        assert item.size_recommendation is not None
        assert item.composite_score >= 0
