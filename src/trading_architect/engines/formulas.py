"""Core sizing formulas — PRD Appendix A."""

from __future__ import annotations

from trading_architect.config.defaults import OPTION_CONTRACT_MULTIPLIER


def dollar_risk_stock(entry: float, stop: float, qty: float, multiplier: float = 1.0) -> float:
    """A.1 — dollar risk for stock/futures leg."""
    return abs(entry - stop) * qty * multiplier


def fractional_risk_size(
    silo_equity: float,
    f: float,
    risk_per_unit: float,
) -> float:
    """A.2 — units/contracts from fractional risk budget."""
    if risk_per_unit <= 0:
        return 0.0
    return (f * silo_equity) / risk_per_unit


def r_multiple(realized_pnl: float, initial_risk: float) -> float | None:
    """A.3 — outcome in R units."""
    if initial_risk <= 0:
        return None
    return realized_pnl / initial_risk


def delta_adjusted_notional(
    shares: float,
    spot: float,
    option_contracts: list[tuple[float, float]],
) -> float:
    """A.4 — combined delta-adjusted notional.

    option_contracts: list of (contracts, delta) pairs.
    """
    stock_notional = shares * spot
    option_notional = sum(
        contracts * delta * OPTION_CONTRACT_MULTIPLIER * spot
        for contracts, delta in option_contracts
    )
    return stock_notional + option_notional


def portfolio_heat(total_open_risk: float, silo_equity: float) -> float:
    """A.8 — heat as fraction of silo equity."""
    if silo_equity <= 0:
        return 0.0
    return total_open_risk / silo_equity


def leverage_ratio(total_notional: float, silo_equity: float) -> float:
    """A.8 — delta-notional leverage multiple."""
    if silo_equity <= 0:
        return 0.0
    return total_notional / silo_equity
