"""Load local .env into process environment (optional dependency)."""

from __future__ import annotations

import os
from pathlib import Path

from trading_architect.config.defaults import PROJECT_ROOT

_LOADED = False


def load_env() -> None:
    """Load PROJECT_ROOT/.env once per process. No-op if file or dotenv missing."""
    global _LOADED
    if _LOADED:
        return

    env_path = PROJECT_ROOT / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
        except ImportError:
            _load_from_file(env_path)

    _LOADED = True


def _load_from_file(path: Path) -> None:
    """Minimal .env parser when python-dotenv is not installed."""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'").rstrip(";")
        if key and key not in os.environ:
            os.environ[key] = value


def rh_credentials_configured() -> bool:
    load_env()
    return bool(os.environ.get("RH_USERNAME") and os.environ.get("RH_PASSWORD"))


def schwab_credentials_configured() -> bool:
    load_env()
    return bool(os.environ.get("SCHWAB_API_KEY") and os.environ.get("SCHWAB_APP_SECRET"))


def schwab_credentials_hint() -> str | None:
    """Actionable hint when Schwab keys are missing (unsaved .env, wrong names, etc.)."""
    if schwab_credentials_configured():
        return None

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return f"Create {env_path} with SCHWAB_API_KEY and SCHWAB_APP_SECRET (see .env.example)."

    text = env_path.read_text(encoding="utf-8")
    has_key_line = any(
        line.strip().startswith("SCHWAB_API_KEY=") and not line.strip().startswith("#")
        for line in text.splitlines()
    )
    has_secret_line = any(
        line.strip().startswith("SCHWAB_APP_SECRET=") and not line.strip().startswith("#")
        for line in text.splitlines()
    )

    if not has_key_line or not has_secret_line:
        return (
            f"Add SCHWAB_API_KEY and SCHWAB_APP_SECRET to {env_path} "
            "(see .env.example), then save the file."
        )

    return (
        f"SCHWAB_* entries exist in {env_path} but are not loaded. "
        "If you edited .env in the IDE, save the file (Cmd+S) and run the command again."
    )
