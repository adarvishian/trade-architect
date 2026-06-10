"""Black-Scholes(-Merton) option pricing — PRD Appendix A.9."""

from __future__ import annotations

import math

from scipy.stats import norm


def _d1_d2(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    vol: float,
) -> tuple[float, float]:
    if time_years <= 0 or vol <= 0:
        return 0.0, 0.0
    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * time_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    return d1, d2


def black_scholes_price(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    vol: float,
    right: str = "C",
) -> float:
    """European option price. Returns intrinsic at expiry (T=0)."""
    if spot <= 0 or strike <= 0:
        return 0.0

    is_call = right.upper().startswith("C")
    if time_years <= 0:
        intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
        return intrinsic

    if vol <= 0:
        forward = spot * math.exp(rate * time_years)
        intrinsic = max(0.0, forward - strike) if is_call else max(0.0, strike - forward)
        return math.exp(-rate * time_years) * intrinsic

    d1, d2 = _d1_d2(spot, strike, time_years, rate, vol)
    if is_call:
        return spot * norm.cdf(d1) - strike * math.exp(-rate * time_years) * norm.cdf(d2)
    return strike * math.exp(-rate * time_years) * norm.cdf(-d2) - spot * norm.cdf(-d1)


def implied_vol_bisect(
    market_price: float,
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    right: str = "C",
    *,
    low: float = 0.01,
    high: float = 3.0,
    tol: float = 1e-4,
    max_iter: int = 80,
) -> float | None:
    """Solve IV from market mid when chain IV is missing."""
    if market_price <= 0 or time_years <= 0:
        return None

    lo, hi = low, high
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        px = black_scholes_price(spot, strike, time_years, rate, mid, right)
        if abs(px - market_price) < tol:
            return mid
        if px > market_price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def bs_greeks(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    vol: float,
    right: str = "C",
) -> dict[str, float]:
    """Closed-form Black-Scholes greeks (per share, theta per calendar day)."""
    if spot <= 0 or strike <= 0 or time_years <= 0 or vol <= 0:
        is_call = right.upper().startswith("C")
        if time_years <= 0:
            delta = 1.0 if (is_call and spot > strike) else (-1.0 if (not is_call and spot < strike) else 0.0)
            return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    is_call = right.upper().startswith("C")
    d1, d2 = _d1_d2(spot, strike, time_years, rate, vol)
    sqrt_t = math.sqrt(time_years)
    pdf_d1 = norm.pdf(d1)
    discount = math.exp(-rate * time_years)

    gamma = pdf_d1 / (spot * vol * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0  # per 1% vol move

    if is_call:
        delta = norm.cdf(d1)
        theta = (
            -spot * pdf_d1 * vol / (2 * sqrt_t)
            - rate * strike * discount * norm.cdf(d2)
        ) / 365.0
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (
            -spot * pdf_d1 * vol / (2 * sqrt_t)
            + rate * strike * discount * norm.cdf(-d2)
        ) / 365.0

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}
