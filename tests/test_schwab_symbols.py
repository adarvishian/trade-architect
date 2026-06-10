"""Tests for Schwab OCC symbol conversion."""

import pytest

from trading_architect.ingestion.schwab_symbols import from_occ, to_occ


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
