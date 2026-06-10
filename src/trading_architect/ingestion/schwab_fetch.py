"""Backward-compatible re-exports — prefer ``schwab_transactions`` for new code."""

from trading_architect.ingestion.schwab_transactions import (
    fetch_all,
    parse_transactions_response,
)

__all__ = ["fetch_all", "parse_transactions_response"]
