"""Bidirectional OCC ↔ internal option symbol conversion (PRD §6)."""

from __future__ import annotations

import re
from datetime import date, datetime

_SYNTHETIC_OPTION = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})_(?P<exp>\d{4}-\d{2}-\d{2})_(?P<right>[CP])_(?P<strike>[\d.]+)$"
)
_OCC_COMPACT = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$", re.IGNORECASE)


def to_occ(internal_symbol: str) -> str:
    """Convert internal option symbol to Schwab OCC format.

    Example: AAPL_2026-09-18_C_110.0 → AAPL  260918C00110000
    """
    match = _SYNTHETIC_OPTION.match(internal_symbol.strip().upper())
    if not match:
        return internal_symbol.upper()

    ticker = match.group("ticker")
    exp = date.fromisoformat(match.group("exp"))
    right = match.group("right").upper()
    strike = float(match.group("strike"))
    root = ticker.ljust(6)
    exp_code = exp.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    return f"{root}{exp_code}{right}{strike_int:08d}"


def from_occ(occ: str) -> str | None:
    """Convert Schwab OCC symbol to internal synthetic format."""
    text = occ.strip().upper()
    compact = re.sub(r"\s+", "", text)
    match = _OCC_COMPACT.match(compact)
    if not match:
        if len(text) < 15:
            return None
        root = text[:6].strip()
        exp_code = text[6:12]
        right = text[12]
        strike_raw = text[13:21]
    else:
        root = match.group(1)
        exp_code = match.group(2)
        right = match.group(3).upper()
        strike_raw = match.group(4)
    try:
        expiry = datetime.strptime(exp_code, "%y%m%d").date()
        strike = int(strike_raw) / 1000.0
    except ValueError:
        return None
    return f"{root}_{expiry.isoformat()}_{right}_{strike}"


# Backward-compatible aliases used by schwab_market_data and tests.
synthetic_to_occ = to_occ
occ_to_synthetic = from_occ
