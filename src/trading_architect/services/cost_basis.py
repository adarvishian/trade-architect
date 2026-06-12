"""Normalize option cost basis to per-share units across brokers."""

from __future__ import annotations

from trading_architect.config.defaults import OPTION_CONTRACT_MULTIPLIER
from trading_architect.models.entities import AssetType
from trading_architect.store.repository import AccountKind

_PER_CONTRACT_BROKERS = frozenset({"robinhood"})


def normalize_option_cost_per_share(
    cost_basis: float,
    asset_type: AssetType | str,
    *,
    broker_kind: AccountKind | None = None,
) -> float:
    """Robinhood reports per-contract premium; Schwab reports per-share."""
    if isinstance(asset_type, str):
        asset_type = AssetType(asset_type)
    if asset_type != AssetType.OPTION:
        return cost_basis
    if broker_kind in _PER_CONTRACT_BROKERS and abs(cost_basis) >= 1.0:
        return cost_basis / OPTION_CONTRACT_MULTIPLIER
    return cost_basis
