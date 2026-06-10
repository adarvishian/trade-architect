"""Schwab Trader API OAuth session — single accessor for all Schwab modules."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from trading_architect.config.defaults import DATA_DIR
from trading_architect.config.env import (
    load_env,
    schwab_credentials_configured,
    schwab_credentials_hint,
)

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_PATH = DATA_DIR / ".schwab_token.json"
_REFRESH_TOKEN_TTL_SECONDS = 7 * 24 * 3600
_MIN_REQUEST_INTERVAL = 0.5
_user_preference_cache: StreamerInfo | None = None


class SchwabAuthExpired(Exception):
    """Refresh token expired or invalid; user must complete OAuth again."""

    RECONNECT_MESSAGE = (
        "Schwab session expired. Reconnect with `ta fetch-schwab --login` "
        "or use reauthenticate() in Settings."
    )

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.RECONNECT_MESSAGE)


@dataclass(frozen=True)
class StreamerInfo:
    streamer_socket_url: str
    schwab_client_customer_id: str
    schwab_client_correl_id: str
    schwab_client_channel: str
    schwab_client_function_id: str


def _require_schwab_py() -> None:
    try:
        import schwab  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "schwab-py is required for Schwab API access. "
            'Install with: pip install -e ".[schwab]"'
        ) from exc


def schwab_py_available() -> bool:
    try:
        _require_schwab_py()
        return True
    except ImportError:
        return False


def default_token_path() -> Path:
    load_env()
    custom = os.environ.get("SCHWAB_TOKEN_PATH")
    if custom:
        return Path(custom)
    return DEFAULT_TOKEN_PATH


def token_file_present() -> bool:
    return default_token_path().is_file()


def _chmod_token_file(path: Path) -> None:
    if path.is_file():
        path.chmod(0o600)


def _secure_token_write(path: Path):
    def write_token(data: dict, *args: Any, **kwargs: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        _chmod_token_file(path)

    return write_token


def _api_credentials() -> tuple[str, str, str]:
    load_env()
    api_key = os.environ.get("SCHWAB_API_KEY", "")
    app_secret = os.environ.get("SCHWAB_APP_SECRET", "")
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    if not api_key or not app_secret:
        hint = schwab_credentials_hint()
        msg = "Set SCHWAB_API_KEY and SCHWAB_APP_SECRET in .env for Schwab API access."
        if hint:
            msg = f"{msg} {hint}"
        raise ValueError(msg)
    return api_key, app_secret, callback_url


def _is_auth_failure(exc: BaseException) -> bool:
    if isinstance(exc, SchwabAuthExpired):
        return True
    try:
        from authlib.integrations.base_client.errors import OAuthError
    except ImportError:
        OAuthError = ()  # type: ignore[misc, assignment]
    if isinstance(exc, OAuthError):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        return True
    message = str(exc).lower()
    return "invalid_grant" in message or "refresh" in message and "token" in message


def _raise_for_response_auth(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise SchwabAuthExpired()


def _load_token_bundle() -> dict[str, Any] | None:
    path = default_token_path()
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _access_expires_at(token: dict[str, Any]) -> datetime | None:
    expires_at = token.get("expires_at")
    if expires_at is not None:
        return datetime.fromtimestamp(float(expires_at), tz=timezone.utc)
    return None


def token_status() -> dict[str, bool | datetime | int | None]:
    """Token file presence and expiry metadata for Settings UI."""
    bundle = _load_token_bundle()
    if not bundle:
        return {
            "present": False,
            "access_expires_at": None,
            "refresh_expires_at": None,
            "days_left": None,
        }

    token = bundle.get("token", bundle)
    creation = bundle.get("creation_timestamp")
    now = datetime.now(timezone.utc)
    access_expires_at = _access_expires_at(token) if isinstance(token, dict) else None

    refresh_expires_at: datetime | None = None
    days_left: int | None = None
    if creation is not None:
        refresh_expires_at = datetime.fromtimestamp(
            int(creation) + _REFRESH_TOKEN_TTL_SECONDS,
            tz=timezone.utc,
        )
        days_left = max(0, (refresh_expires_at - now).days)

    return {
        "present": True,
        "access_expires_at": access_expires_at,
        "refresh_expires_at": refresh_expires_at,
        "days_left": days_left,
    }


class SchwabSession:
    """Authenticated Schwab Trader API session with throttling and auth mapping."""

    def __init__(self, client: Any, *, min_interval: float = _MIN_REQUEST_INTERVAL) -> None:
        self._client = client
        self._min_interval = min_interval
        self._last_call = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def get_user_preferences(self) -> httpx.Response:
        self._wait()
        try:
            resp = self._client.get_user_preferences()
        except Exception as exc:
            if _is_auth_failure(exc):
                raise SchwabAuthExpired() from exc
            raise
        _raise_for_response_auth(resp)
        return resp

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        # Nested API types (e.g. Account.Fields) must not be wrapped — types are callable.
        if isinstance(attr, type) or not callable(attr):
            return attr

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            self._wait()
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:
                if _is_auth_failure(exc):
                    raise SchwabAuthExpired() from exc
                raise
            if isinstance(result, httpx.Response):
                _raise_for_response_auth(result)
            return result

        return wrapped


def _build_client(*, interactive: bool) -> Any:
    from schwab.auth import (
        client_from_access_functions,
        client_from_login_flow,
    )

    api_key, app_secret, callback_url = _api_credentials()
    token_path = default_token_path()
    token_read = _token_loader(token_path)
    token_write = _secure_token_write(token_path)

    if token_path.is_file():
        try:
            return client_from_access_functions(
                api_key,
                app_secret,
                token_read,
                token_write,
            )
        except Exception as exc:
            if _is_auth_failure(exc):
                raise SchwabAuthExpired() from exc
            raise

    if not interactive:
        raise FileNotFoundError(
            f"No Schwab token at {token_path}. "
            "Run `ta fetch-schwab --login` once to authenticate, "
            "or set SCHWAB_TOKEN_PATH to an existing token file."
        )

    try:
        return client_from_login_flow(
            api_key,
            app_secret,
            callback_url,
            token_path,
            token_write_func=token_write,
        )
    except Exception as exc:
        if _is_auth_failure(exc):
            raise SchwabAuthExpired() from exc
        raise


def _token_loader(path: Path):
    def load_token() -> dict[str, Any]:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    return load_token


def get_client(*, interactive: bool = False, client: Any | None = None) -> SchwabSession:
    """Return a ready Schwab session; refresh access token via schwab-py when needed."""
    if client is not None:
        return SchwabSession(client)

    _require_schwab_py()
    raw = _build_client(interactive=interactive)
    _chmod_token_file(default_token_path())
    return SchwabSession(raw)


def reauthenticate() -> SchwabSession:
    """Restart the three-legged OAuth flow (dead or revoked refresh token)."""
    from schwab.auth import client_from_manual_flow

    _require_schwab_py()
    api_key, app_secret, callback_url = _api_credentials()
    token_path = default_token_path()
    token_write = _secure_token_write(token_path)

    try:
        raw = client_from_manual_flow(
            api_key,
            app_secret,
            callback_url,
            token_path,
            token_write_func=token_write,
        )
    except Exception as exc:
        if _is_auth_failure(exc):
            raise SchwabAuthExpired() from exc
        raise

    global _user_preference_cache
    _user_preference_cache = None
    _chmod_token_file(token_path)
    return SchwabSession(raw)


def _streamer_info_from_preferences(payload: dict[str, Any]) -> StreamerInfo:
    entries = payload.get("streamerInfo") or []
    if not entries:
        raise ValueError("userPreference response missing streamerInfo")
    info = entries[0]
    return StreamerInfo(
        streamer_socket_url=str(info["streamerSocketUrl"]),
        schwab_client_customer_id=str(info["schwabClientCustomerId"]),
        schwab_client_correl_id=str(info["schwabClientCorrelId"]),
        schwab_client_channel=str(info["schwabClientChannel"]),
        schwab_client_function_id=str(info["schwabClientFunctionId"]),
    )


def get_user_preference(
    *,
    client: SchwabSession | None = None,
    refresh: bool = False,
) -> StreamerInfo:
    """Fetch and cache Streamer LOGIN parameters from GET /trader/v1/userPreference."""
    global _user_preference_cache
    if _user_preference_cache is not None and not refresh:
        return _user_preference_cache

    session = client or get_client()
    resp = session.get_user_preferences()
    if resp.status_code != httpx.codes.OK:
        raise RuntimeError(f"Failed to load Schwab userPreference: {resp.status_code}")

    _user_preference_cache = _streamer_info_from_preferences(resp.json())
    return _user_preference_cache


def schwab_connection_status() -> dict[str, str | bool | int | None]:
    """Human-readable connection status for CLI and Streamlit."""
    load_env()
    configured = schwab_credentials_configured()
    status = token_status()
    token = bool(status["present"])
    installed = schwab_py_available()

    if not installed:
        return {
            "installed": False,
            "configured": configured,
            "token": token,
            "message": "schwab-py not installed",
            **status,
        }
    if not configured:
        return {
            "installed": True,
            "configured": False,
            "token": token,
            "message": "Set SCHWAB_API_KEY and SCHWAB_APP_SECRET in .env",
            **status,
        }
    if not token:
        return {
            "installed": True,
            "configured": True,
            "token": False,
            "message": "Credentials set but no token file — run `ta fetch-schwab --login` once",
            **status,
        }

    days_left = status.get("days_left")
    if days_left is not None:
        message = f"Connected — refresh token expires in {days_left} day(s)"
    else:
        message = f"Token file present ({default_token_path()})"

    return {
        "installed": True,
        "configured": True,
        "token": True,
        "message": message,
        **status,
    }
