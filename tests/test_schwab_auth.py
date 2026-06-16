"""Phase 0 — Schwab OAuth session layer tests (no live network)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from trading_architect.ingestion import schwab_auth
from trading_architect.ingestion.schwab_auth import (
    SchwabAuthExpired,
    SchwabSession,
    get_user_preference,
    token_status,
)


@pytest.fixture(autouse=True)
def _clear_preference_cache():
    schwab_auth._user_preference_cache = None
    yield
    schwab_auth._user_preference_cache = None


def test_token_status_empty(tmp_path, monkeypatch):
    token_path = tmp_path / ".schwab_token.json"
    monkeypatch.setattr(schwab_auth, "default_token_path", lambda: token_path)

    status = token_status()
    assert status["present"] is False
    assert status["days_left"] is None


def test_token_status_populated(tmp_path, monkeypatch):
    token_path = tmp_path / ".schwab_token.json"
    now = int(time.time())
    bundle = {
        "creation_timestamp": now - 86400,
        "token": {
            "access_token": "redacted",
            "refresh_token": "redacted",
            "expires_at": now + 1800,
        },
    }
    token_path.write_text(json.dumps(bundle), encoding="utf-8")
    token_path.chmod(0o600)
    monkeypatch.setattr(schwab_auth, "default_token_path", lambda: token_path)

    status = token_status()
    assert status["present"] is True
    assert status["access_expires_at"] is not None
    assert status["refresh_expires_at"] is not None
    assert status["days_left"] is not None
    assert status["days_left"] >= 5


def test_secure_token_write_sets_mode(tmp_path):
    path = tmp_path / "token.json"
    write = schwab_auth._secure_token_write(path)
    write({"token": {"access_token": "x"}})
    assert path.stat().st_mode & 0o777 == 0o600


def test_get_user_preference_caches(monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "schwab" / "user_preference.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    mock_client = MagicMock()
    mock_client.get_user_preferences.return_value = httpx.Response(
        200,
        json=payload,
    )
    session = SchwabSession(mock_client)

    info1 = get_user_preference(client=session)
    info2 = get_user_preference(client=session)
    assert info1 == info2
    mock_client.get_user_preferences.assert_called_once()
    assert info1.streamer_socket_url == "wss://streamer.example.test/ws"
    assert info1.schwab_client_correl_id == "correl-abc-123"


def test_session_exposes_nested_account_fields_enum():
    """Account.Fields must not be wrapped (types are callable in Python)."""
    from enum import Enum

    class AccountFields(Enum):
        POSITIONS = "positions"

    class Account:
        Fields = AccountFields

    mock_client = MagicMock()
    mock_client.Account = Account
    session = SchwabSession(mock_client)

    assert session.Account.Fields.POSITIONS is AccountFields.POSITIONS
    assert session.Account is Account


def test_session_maps_401_to_schwab_auth_expired():
    mock_client = MagicMock()
    mock_client.get_user_preferences.return_value = httpx.Response(401)
    session = SchwabSession(mock_client)

    with pytest.raises(SchwabAuthExpired):
        session.get_user_preferences()


def test_schwab_auth_expired_message():
    exc = SchwabAuthExpired()
    assert "Reconnect" in str(exc) or "login" in str(exc).lower()


def test_build_client_interactive_runs_login_flow(tmp_path, monkeypatch):
    """--login must run OAuth even when an expired token file already exists."""
    token_path = tmp_path / ".schwab_token.json"
    token_path.write_text('{"creation_timestamp": 1, "token": {"access_token": "old"}}')
    monkeypatch.setattr(schwab_auth, "default_token_path", lambda: token_path)
    monkeypatch.setenv("SCHWAB_API_KEY", "key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "secret")

    login_client = MagicMock()
    access_client = MagicMock()

    def fake_login_flow(*args, **kwargs):
        return login_client

    def fake_access_functions(*args, **kwargs):
        return access_client

    monkeypatch.setattr(
        "schwab.auth.client_from_login_flow",
        fake_login_flow,
    )
    monkeypatch.setattr(
        "schwab.auth.client_from_access_functions",
        fake_access_functions,
    )

    client = schwab_auth._build_client(interactive=True)
    assert client is login_client
    access_client.assert_not_called()
    login_client.assert_not_called()


def test_schwab_connection_status_reports_expired_refresh(tmp_path, monkeypatch):
    token_path = tmp_path / ".schwab_token.json"
    expired = int(time.time()) - 86400
    bundle = {
        "creation_timestamp": expired - 7 * 86400,
        "token": {
            "access_token": "redacted",
            "refresh_token": "redacted",
            "expires_at": expired,
        },
    }
    token_path.write_text(json.dumps(bundle), encoding="utf-8")
    monkeypatch.setattr(schwab_auth, "default_token_path", lambda: token_path)
    monkeypatch.setattr(schwab_auth, "schwab_py_available", lambda: True)
    monkeypatch.setattr(
        "trading_architect.config.env.schwab_credentials_configured",
        lambda: True,
    )

    status = schwab_auth.schwab_connection_status()
    assert "expired" in str(status["message"]).lower()


def test_token_status_never_exposes_secrets(tmp_path, monkeypatch, caplog):
    token_path = tmp_path / ".schwab_token.json"
    secret = "super-secret-access-token-value"
    bundle = {
        "creation_timestamp": int(time.time()),
        "token": {
            "access_token": secret,
            "refresh_token": "super-secret-refresh",
            "expires_at": int(time.time()) + 3600,
        },
    }
    token_path.write_text(json.dumps(bundle), encoding="utf-8")
    monkeypatch.setattr(schwab_auth, "default_token_path", lambda: token_path)

    with caplog.at_level("DEBUG"):
        status = token_status()

    assert secret not in str(status)
    assert secret not in caplog.text
