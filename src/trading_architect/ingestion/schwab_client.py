"""Backward-compatible re-exports — prefer ``schwab_auth`` for new code."""

from trading_architect.ingestion.schwab_auth import (
    SchwabAuthExpired,
    SchwabSession,
    StreamerInfo,
    default_token_path,
    get_client,
    get_user_preference,
    reauthenticate,
    schwab_connection_status,
    schwab_py_available,
    token_file_present,
    token_status,
)

__all__ = [
    "SchwabAuthExpired",
    "SchwabSession",
    "StreamerInfo",
    "default_token_path",
    "get_client",
    "get_user_preference",
    "reauthenticate",
    "schwab_connection_status",
    "schwab_py_available",
    "token_file_present",
    "token_status",
]
