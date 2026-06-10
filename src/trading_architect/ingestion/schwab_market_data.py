"""Backward-compatible re-exports — prefer ``schwab_rest`` for new code."""

from __future__ import annotations

import re
from datetime import date

from trading_architect.ingestion.schwab_rest import (
    fetch_chain_snapshot,
    fetch_quotes,
    parse_option_chain_response,
    parse_quotes_response,
)
from trading_architect.ingestion.schwab_symbols import from_occ as occ_to_synthetic
from trading_architect.ingestion.schwab_symbols import to_occ as synthetic_to_occ
from trading_architect.models.entities import OptionSpec

_SYNTHETIC_OPTION = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})_(?P<exp>\d{4}-\d{2}-\d{2})_(?P<right>[CP])_(?P<strike>[\d.]+)$"
)


def parse_synthetic_option(symbol: str) -> tuple[str, str, OptionSpec] | None:
    """Parse canonical synthetic option symbol TICKER_YYYY-MM-DD_R_strike."""
    match = _SYNTHETIC_OPTION.match(symbol.strip().upper())
    if not match:
        return None
    ticker = match.group("ticker")
    expiry = date.fromisoformat(match.group("exp"))
    right = match.group("right").upper()
    strike = float(match.group("strike"))
    canonical = f"{ticker}_{expiry.isoformat()}_{right}_{strike}"
    return ticker, canonical, OptionSpec(right=right, strike=strike, expiry=expiry)


__all__ = [
    "fetch_chain_snapshot",
    "fetch_quotes",
    "occ_to_synthetic",
    "parse_option_chain_response",
    "parse_quotes_response",
    "parse_synthetic_option",
    "synthetic_to_occ",
]
