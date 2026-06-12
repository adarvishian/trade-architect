"""Streamlit AppTest smoke coverage — all pages render without exception."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
APP_FILE = APP_DIR / "streamlit_app.py"

PAGES = [
    "Dashboard",
    "Size a Trade",
    "Accounts",
    "Settings",
]


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)
    yield db_path


@pytest.fixture
def seeded_db(temp_db):
    from trading_architect.bootstrap import import_and_assemble

    fixtures = Path(__file__).parent / "fixtures"
    import_and_assemble(fixtures / "robinhood_sample.csv", "robinhood", account="ui-test")
    return temp_db


@pytest.fixture
def streamlit_app_env(seeded_db, monkeypatch):
    monkeypatch.chdir(APP_DIR)
    monkeypatch.syspath_prepend(str(APP_DIR))
    monkeypatch.syspath_prepend(str(ROOT / "src"))


def test_all_pages_render(streamlit_app_env):
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_FILE))
    at.run(timeout=60)
    assert not at.exception, at.exception

    for page in PAGES[1:]:
        at.sidebar.radio[0].set_value(page).run(timeout=60)
        assert not at.exception, f"{page}: {at.exception}"


def test_dashboard_shows_metrics_with_data(streamlit_app_env):
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_FILE))
    at.run(timeout=60)
    assert not at.exception
    assert any("Total capital" in m.label or "Deployable" in m.label for m in at.metric)
