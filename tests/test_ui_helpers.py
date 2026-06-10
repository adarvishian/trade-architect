"""Tests for Streamlit UI error formatting."""

from __future__ import annotations

from ui_helpers import format_ui_error


def test_schwab_auth_expired_shows_reconnect_message():
    from trading_architect.ingestion.schwab_auth import SchwabAuthExpired

    msg = format_ui_error(SchwabAuthExpired(), context="test")
    assert "fetch-schwab --login" in msg


def test_network_error_is_user_friendly():
    msg = format_ui_error(ConnectionError("refused"), context="portfolio fetch")
    assert "Network error" in msg
    assert "refused" not in msg


def test_parse_error_is_user_friendly():
    msg = format_ui_error(ValueError("bad field"), context="import")
    assert "Could not parse data" in msg
    assert "bad field" not in msg


def test_unknown_error_logged_not_shown(tmp_path, monkeypatch):
    from trading_architect.config import defaults

    log_path = tmp_path / "ui.log"
    monkeypatch.setattr(defaults, "DATA_DIR", tmp_path)
    monkeypatch.setattr("ui_helpers.DATA_DIR", tmp_path)
    monkeypatch.setattr("ui_helpers.UI_LOG_PATH", log_path)

    msg = format_ui_error(RuntimeError("secret internals"), context="mystery op")
    assert "unexpected error" in msg.lower()
    assert "secret internals" not in msg
    assert log_path.exists()
    assert "secret internals" in log_path.read_text()


def test_import_error_passes_through():
    msg = format_ui_error(ImportError("no module robin_stocks"), context="fetch")
    assert "robin_stocks" in msg
