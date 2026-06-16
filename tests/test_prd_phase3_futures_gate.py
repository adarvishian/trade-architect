"""PRD Phase 3 — defer futures for v1 via show_futures settings gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_architect.bootstrap import import_and_assemble
from trading_architect.config.user_settings import (
    AppSettings,
    app_settings_from_json,
    app_settings_to_json,
)
from trading_architect.models.entities import Silo
from trading_architect.store.database import Database
from trading_architect.store.repository import Repository

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
APP_FILE = APP_DIR / "streamlit_app.py"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "phase3_futures.db"))


def test_show_futures_defaults_off():
    settings = AppSettings()
    assert settings.show_futures is False


def test_show_futures_settings_roundtrip(repo: Repository):
    settings = AppSettings(show_futures=True, starting_equity_futures=75_000.0)
    repo.save_app_settings(settings)
    loaded = repo.load_app_settings()
    assert loaded.show_futures is True
    assert loaded.starting_equity_futures == 75_000.0

    raw = app_settings_to_json(loaded)
    restored = app_settings_from_json(raw)
    assert restored.show_futures is True


def test_ui_helpers_hide_futures_when_gate_off():
    from ui_helpers import asset_choices, silo_choices, silo_exposure_rows

    from trading_architect.engines.book_context import build_book_context

    hidden = AppSettings(show_futures=False)
    visible = AppSettings(show_futures=True)

    assert silo_choices(hidden) == ["stock_options"]
    assert silo_choices(visible) == ["stock_options", "futures"]
    assert asset_choices(hidden) == ["stock", "option"]
    assert asset_choices(visible) == ["stock", "option", "future"]

    book = build_book_context([], [], hidden)
    rows = silo_exposure_rows(book, hidden)
    assert len(rows) == 1
    assert rows[0]["silo"] == "stock_options"

    rows_on = silo_exposure_rows(book, visible)
    assert len(rows_on) == 2
    assert {r["silo"] for r in rows_on} == {"stock_options", "futures"}


def test_tradovate_import_preserves_futures_data_when_gate_off(tmp_path, monkeypatch):
    db_path = tmp_path / "tradovate.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)

    result = import_and_assemble(
        FIXTURES / "tradovate_sample.csv",
        broker="tradovate",
        account="tradovate-test",
    )
    assert result.imported > 0

    from trading_architect.bootstrap import create_repository

    repo = create_repository()
    events = repo.list_events()
    positions = repo.list_positions()
    fut_events = [e for e in events if e.silo == Silo.FUTURES]
    fut_positions = [p for p in positions if p.silo == Silo.FUTURES]
    assert len(fut_events) > 0
    assert len(fut_positions) > 0

    settings = repo.load_app_settings()
    assert settings.show_futures is False


@pytest.fixture
def streamlit_app_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)
    monkeypatch.chdir(APP_DIR)
    monkeypatch.syspath_prepend(str(APP_DIR))
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    import_and_assemble(FIXTURES / "robinhood_sample.csv", "robinhood", account="ui-test")
    yield db_path


def test_dashboard_hides_futures_metric_when_gate_off(streamlit_app_env):
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_FILE))
    at.run(timeout=60)
    assert not at.exception, at.exception

    labels = [m.label for m in at.metric]
    assert not any("Futures silo" in label for label in labels)
    assert any("Stock/options silo" in label for label in labels)


def test_dashboard_shows_futures_metric_when_gate_on(tmp_path, monkeypatch):
    pytest.importorskip("streamlit.testing.v1")
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_resource.clear()
    st.cache_data.clear()

    db_path = tmp_path / "futures_on.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)
    monkeypatch.chdir(APP_DIR)
    monkeypatch.syspath_prepend(str(APP_DIR))
    monkeypatch.syspath_prepend(str(ROOT / "src"))

    import_and_assemble(FIXTURES / "robinhood_sample.csv", "robinhood", account="ui-test")

    from trading_architect.bootstrap import create_repository
    from trading_architect.config.user_settings import AppSettings

    repo = create_repository()
    repo.save_app_settings(AppSettings(show_futures=True))
    assert repo.load_app_settings().show_futures is True

    at = AppTest.from_file(str(APP_FILE))
    at.run(timeout=60)
    assert not at.exception, at.exception

    labels = [m.label for m in at.metric]
    assert any("Futures silo" in label for label in labels), labels
