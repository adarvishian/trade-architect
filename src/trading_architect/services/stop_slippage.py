"""Slippage-honest stop risk — Phase 4."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from trading_architect.assembly.entries import extract_closed_entries
from trading_architect.models.entities import AssetType, Direction, Silo
from trading_architect.store.repository import Repository

MIN_OBSERVATIONS = 10


@dataclass(frozen=True)
class SlippageCalibration:
    pads_by_asset_type: dict[str, float]
    observation_counts: dict[str, int]
    status: str  # calibrating | calibrated


def _exit_slippage_pct(
    *,
    direction: Direction,
    exit_price: float,
    stop_price: float,
) -> float | None:
    if stop_price <= 0:
        return None
    if direction == Direction.LONG:
        if exit_price <= stop_price:
            return (stop_price - exit_price) / stop_price
        return 0.0
    if exit_price >= stop_price:
        return (exit_price - stop_price) / stop_price
    return 0.0


def collect_slippage_observations(repo: Repository) -> dict[str, list[float]]:
    """Compare exit fills to recorded stop at exit time."""
    events = repo.list_events()
    closed = extract_closed_entries(events)
    by_type: dict[str, list[float]] = {}

    for entry in closed:
        if entry.asset_type not in (AssetType.STOCK, AssetType.FUTURE):
            continue
        stop = repo.stop_at_time(entry.symbol, entry.silo, entry.closed_at)
        if stop is None:
            stop = repo.stop_at_time(entry.underlying, entry.silo, entry.closed_at)
        if stop is None:
            continue
        slip = _exit_slippage_pct(
            direction=entry.direction,
            exit_price=entry.exit_price,
            stop_price=stop,
        )
        if slip is None or slip <= 0:
            continue
        key = entry.asset_type.value
        by_type.setdefault(key, []).append(slip)

    return by_type


def slippage_calibration(repo: Repository) -> SlippageCalibration:
    obs = collect_slippage_observations(repo)
    pads: dict[str, float] = {}
    counts: dict[str, int] = {}
    any_calibrated = False
    for asset_type, values in obs.items():
        counts[asset_type] = len(values)
        if len(values) >= MIN_OBSERVATIONS:
            pads[asset_type] = statistics.median(values)
            any_calibrated = True
        else:
            pads[asset_type] = 0.0
    return SlippageCalibration(
        pads_by_asset_type=pads,
        observation_counts=counts,
        status="calibrated" if any_calibrated else "calibrating",
    )


def adjusted_stop_risk(
    raw_stop_risk: float,
    asset_type: str,
    calibration: SlippageCalibration,
) -> float:
    pad = calibration.pads_by_asset_type.get(asset_type, 0.0)
    if calibration.status == "calibrating" or pad <= 0:
        return raw_stop_risk
    return raw_stop_risk * (1.0 + pad)


def portfolio_slippage_adjusted_heat(
    positions: list,
    repo: Repository,
    calibration: SlippageCalibration | None = None,
) -> tuple[float, float]:
    """Return (raw_stop_risk_sum, slippage_adjusted_stop_risk_sum)."""
    calibration = calibration or slippage_calibration(repo)
    raw = 0.0
    adjusted = 0.0
    for pos in positions:
        raw += pos.stop_risk
        primary_type = "option" if pos.premium_at_risk > pos.stop_risk else "stock"
        if pos.silo == Silo.FUTURES:
            primary_type = "future"
        adjusted += adjusted_stop_risk(pos.stop_risk, primary_type, calibration)
    return raw, adjusted
