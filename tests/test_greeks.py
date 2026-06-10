"""Tests for Black-Scholes greeks."""

import pytest

from trading_architect.engines.options_pricing import black_scholes_price, bs_greeks


def test_bs_greeks_atm_call_textbook_values():
    """S=100, K=100, T=1, r=0.05, σ=0.2 — call delta ≈ 0.6368."""
    g = bs_greeks(100, 100, 1.0, 0.05, 0.2, "C")
    assert g["delta"] == pytest.approx(0.6368, abs=0.001)
    assert g["gamma"] > 0
    assert g["vega"] > 0
    assert g["theta"] < 0


def test_bs_greeks_price_consistency():
    """Delta should approximate dPrice/dSpot for small bump."""
    spot, strike, t, rate, vol = 200, 190, 120 / 365, 0.05, 0.35
    g = bs_greeks(spot, strike, t, rate, vol, "C")
    bump = 0.01
    p_up = black_scholes_price(spot + bump, strike, t, rate, vol, "C")
    p_dn = black_scholes_price(spot - bump, strike, t, rate, vol, "C")
    numerical_delta = (p_up - p_dn) / (2 * bump)
    assert g["delta"] == pytest.approx(numerical_delta, rel=0.05)
