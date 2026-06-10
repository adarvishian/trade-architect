"""Tests for Black-Scholes pricing (PRD A.9)."""

import pytest

from trading_architect.engines.options_pricing import black_scholes_price, implied_vol_bisect


def test_call_price_positive():
    px = black_scholes_price(100, 100, 0.25, 0.05, 0.25, "C")
    assert px > 0


def test_put_call_parity_region():
    call = black_scholes_price(100, 100, 0.5, 0.05, 0.30, "C")
    put = black_scholes_price(100, 100, 0.5, 0.05, 0.30, "P")
    assert call > 0 and put > 0


def test_expiry_intrinsic_call():
    px = black_scholes_price(110, 100, 0.0, 0.05, 0.25, "C")
    assert px == pytest.approx(10.0)


def test_implied_vol_roundtrip():
    vol = 0.35
    px = black_scholes_price(200, 190, 120 / 365, 0.05, vol, "C")
    solved = implied_vol_bisect(px, 200, 190, 120 / 365, 0.05, "C")
    assert solved == pytest.approx(vol, rel=0.02)
