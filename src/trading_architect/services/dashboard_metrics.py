"""Dashboard risk metrics — open-R, theta bleed, expiry runway, concentrations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from trading_architect.config.defaults import OPTION_CONTRACT_MULTIPLIER
from trading_architect.engines.marks import MarksProvider
from trading_architect.models.entities import Position, PositionStatus
from trading_architect.services.position_stops import has_stop

_OPTION_SYMBOL = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})_(?P<exp>\d{4}-\d{2}-\d{2})_(?P<right>[CP])_(?P<strike>[\d.]+)$"
)


@dataclass(frozen=True)
class ExpiryRunway:
    under_30: float
    under_60: float
    under_90: float
    gte_90: float

    @property
    def total(self) -> float:
        return self.under_30 + self.under_60 + self.under_90 + self.gte_90


def _option_symbols(positions: list[Position]) -> list[str]:
    symbols: set[str] = set()
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        for sym, qty in pos.leg_net_qty.items():
            if qty != 0 and "_" in sym:
                symbols.add(sym)
    return sorted(symbols)


def _parse_expiry(symbol: str) -> date | None:
    match = _OPTION_SYMBOL.match(symbol.strip().upper())
    if not match:
        return None
    return date.fromisoformat(match.group("exp"))


def _dte(symbol: str, as_of: date | None = None) -> int | None:
    expiry = _parse_expiry(symbol)
    if expiry is None:
        return None
    ref = as_of or date.today()
    return (expiry - ref).days


def _theta_for_symbol(symbol: str, marks: dict) -> float | None:
    mark = marks.get(symbol)
    if mark is None:
        return None
    return getattr(mark, "theta", None)


def options_theta_day(positions: list[Position], marks_provider: MarksProvider) -> float:
    """Aggregate daily theta bleed: Σ theta × 100 × qty for long option legs."""
    symbols = _option_symbols(positions)
    if not symbols:
        return 0.0
    marks = marks_provider.marks_for(symbols)
    total = 0.0
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        for sym, qty in pos.leg_net_qty.items():
            if qty <= 0 or "_" not in sym:
                continue
            theta = _theta_for_symbol(sym, marks)
            if theta is not None:
                total += theta * OPTION_CONTRACT_MULTIPLIER * qty
    return total


def expiry_runway_premium(
    positions: list[Position],
    *,
    as_of: date | None = None,
) -> ExpiryRunway:
    """Premium-at-risk bucketed by DTE for open long option legs."""
    buckets = ExpiryRunway(0.0, 0.0, 0.0, 0.0)
    ref = as_of or date.today()
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        for sym, qty in pos.leg_net_qty.items():
            if qty <= 0 or "_" not in sym:
                continue
            dte = _dte(sym, ref)
            if dte is None:
                continue
            cost = abs(pos.blended_cost_basis.get(sym, 0.0))
            premium = cost * abs(qty) * OPTION_CONTRACT_MULTIPLIER
            if dte < 30:
                buckets = ExpiryRunway(
                    buckets.under_30 + premium,
                    buckets.under_60,
                    buckets.under_90,
                    buckets.gte_90,
                )
            elif dte < 60:
                buckets = ExpiryRunway(
                    buckets.under_30,
                    buckets.under_60 + premium,
                    buckets.under_90,
                    buckets.gte_90,
                )
            elif dte < 90:
                buckets = ExpiryRunway(
                    buckets.under_30,
                    buckets.under_60,
                    buckets.under_90 + premium,
                    buckets.gte_90,
                )
            else:
                buckets = ExpiryRunway(
                    buckets.under_30,
                    buckets.under_60,
                    buckets.under_90,
                    buckets.gte_90 + premium,
                )
    return buckets


def top_concentrations(
    positions: list[Position],
    *,
    top_n: int = 3,
) -> list[tuple[str, float]]:
    """Top underlyings by delta notional."""
    by_underlying: dict[str, float] = {}
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        by_underlying[pos.underlying] = (
            by_underlying.get(pos.underlying, 0.0) + pos.current_delta_notional
        )
    ranked = sorted(by_underlying.items(), key=lambda x: x[1], reverse=True)
    return ranked[:top_n]


def position_risk_rows(
    positions: list[Position],
    repo,
) -> list[dict]:
    """Rows for dashboard positions table with open-R and no-stop flags."""
    rows: list[dict] = []
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        stop_set = has_stop(pos, repo)
        rows.append(
            {
                "underlying": pos.underlying,
                "silo": pos.silo.value,
                "direction": pos.direction.value,
                "open_r": round(pos.open_r, 2) if pos.open_r is not None else None,
                "no_stop": not stop_set,
                "total_risk": pos.total_dollar_risk,
                "notional": pos.current_delta_notional,
                "premium_at_risk": pos.premium_at_risk,
            }
        )
    return rows


def standard_dollar_risk(equity: float, base_f: float, kelly_fraction: float) -> float:
    """Reference dollar risk at current equity on the configured Kelly path."""
    if equity <= 0:
        return 0.0
    return equity * base_f * kelly_fraction


@dataclass(frozen=True)
class ClusterStress:
    cluster_label: str
    cluster_notional: float
    cluster_pct: float
    stress_gap_dollars: float
    stress_gap_equity_pct: float


def _cluster_label(underlying: str, repo) -> str:
    tag = repo.get_underlying_tag(underlying)
    if tag:
        return tag.sector_tag
    return underlying


def concentration_clusters(
    positions: list[Position],
    repo,
) -> list[tuple[str, float]]:
    """Group open positions by sector tag; return (cluster, delta notional) sorted desc."""
    by_cluster: dict[str, float] = {}
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        label = _cluster_label(pos.underlying, repo)
        by_cluster[label] = by_cluster.get(label, 0.0) + pos.current_delta_notional
    return sorted(by_cluster.items(), key=lambda x: x[1], reverse=True)


def top_cluster_stress(
    positions: list[Position],
    repo,
    *,
    equity: float,
    stress_pct: float = -0.15,
) -> ClusterStress | None:
    """Top cluster share and arithmetic stress line from delta notional."""
    clusters = concentration_clusters(positions, repo)
    if not clusters:
        return None
    total_notional = sum(n for _, n in clusters)
    if total_notional <= 0:
        return None
    label, notional = clusters[0]
    cluster_pct = notional / total_notional
    gap_dollars = notional * stress_pct
    equity_pct = gap_dollars / equity if equity > 0 else 0.0
    return ClusterStress(
        cluster_label=label,
        cluster_notional=notional,
        cluster_pct=cluster_pct,
        stress_gap_dollars=gap_dollars,
        stress_gap_equity_pct=equity_pct,
    )
