"""Phase 2 — position stop overrides and open-R."""

from __future__ import annotations

import pytest

from trading_architect.models.entities import Direction, Position, PositionStatus, Silo
from trading_architect.services.position_stops import (
    apply_position_overrides,
    compute_open_r,
    compute_stop_risk,
    has_stop,
)
from trading_architect.store.database import Database
from trading_architect.store.repository import PositionOverrideRecord, Repository


@pytest.fixture
def repo(tmp_path) -> Repository:
    return Repository(Database(tmp_path / "stops_test.db"))


def _stock_position(**kwargs) -> Position:
    defaults = dict(
        silo=Silo.STOCK_OPTIONS,
        underlying="AAPL",
        direction=Direction.LONG,
        status=PositionStatus.OPEN,
        leg_net_qty={"AAPL": 100.0},
        blended_cost_basis={"AAPL": 150.0},
        current_delta_notional=16_000.0,
    )
    defaults.update(kwargs)
    return Position(**defaults)


def test_stop_risk_from_override():
    pos = _stock_position()
    override = PositionOverrideRecord(
        symbol="AAPL",
        silo=Silo.STOCK_OPTIONS,
        initial_stop=140.0,
        current_stop=140.0,
    )
    assert compute_stop_risk(pos, override) == pytest.approx(1_000.0)


def test_open_r_long():
    pos = _stock_position(current_delta_notional=16_000.0)
    override = PositionOverrideRecord(
        symbol="AAPL",
        silo=Silo.STOCK_OPTIONS,
        initial_stop=140.0,
        current_stop=145.0,
    )
    open_r = compute_open_r(pos, override)
    assert open_r == pytest.approx(1.0)


def test_apply_overrides_updates_heat(repo: Repository):
    pos = _stock_position(premium_at_risk=0.0)
    repo.upsert_position_override(
        symbol="AAPL",
        silo=Silo.STOCK_OPTIONS,
        initial_stop=145.0,
        current_stop=145.0,
    )
    enriched = apply_position_overrides([pos], repo)
    assert enriched[0].stop_risk == pytest.approx(500.0)
    assert enriched[0].total_dollar_risk == pytest.approx(500.0)


def test_has_stop_false_without_override(repo: Repository):
    pos = _stock_position()
    assert has_stop(pos, repo) is False


def test_has_stop_true_with_override(repo: Repository):
    pos = _stock_position()
    repo.upsert_position_override(
        symbol="AAPL",
        silo=Silo.STOCK_OPTIONS,
        initial_stop=140.0,
        current_stop=140.0,
    )
    assert has_stop(pos, repo) is True
