"""Tests for Schwab OCC symbol conversion."""

import pytest

from trading_architect.ingestion.schwab_symbols import from_occ, parse_synthetic_option, to_occ


@pytest.mark.parametrize(
    "internal,occ",
    [
        ("AAPL_2026-09-18_C_110.0", "AAPL  260918C00110000"),
        ("TSLA_2025-12-19_C_400.0", "TSLA  251219C00400000"),
        ("X_2026-01-16_C_25.125", "X     260116C00025125"),
    ],
)
def test_occ_roundtrip(internal, occ):
    assert to_occ(internal) == occ
    assert from_occ(occ) == internal


def test_to_occ_passthrough_equity():
    assert to_occ("AAPL") == "AAPL"


def test_from_occ_compact_no_spaces():
    assert from_occ("TSLA251219C00400000") == "TSLA_2025-12-19_C_400.0"


def test_parse_synthetic_option():
    parsed = parse_synthetic_option("AAPL_2026-09-18_C_110.0")
    assert parsed is not None
    ticker, canonical, spec = parsed
    assert ticker == "AAPL"
    assert canonical == "AAPL_2026-09-18_C_110.0"
    assert spec.strike == 110.0
    assert spec.right == "C"
    assert parse_synthetic_option("not-an-option") is None
