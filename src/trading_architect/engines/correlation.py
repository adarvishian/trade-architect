"""Correlation provider & opportunity clustering — PRD §7.3/§7.7.

Risk is localized to the *opportunity* (a single thesis on a single instrument).
Whether two opportunities are genuinely independent bets is **measured**, not
assumed: correlated opportunities collapse into one effective opportunity for
portfolio-risk attribution and the drawdown governor.

Safe-by-construction default: when no return data is available, distinct
underlyings are assumed *correlated* (default_correlation) so the system stays
as conservative as a uniform governor until data proves independence.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, runtime_checkable

from trading_architect.config.defaults import (
    DEFAULT_ASSUMED_CORRELATION,
    DEFAULT_CORRELATION_THRESHOLD,
)

logger = logging.getLogger(__name__)

# Minimum overlapping observations before a measured correlation is trusted.
DEFAULT_MIN_OBSERVATIONS = 20


@runtime_checkable
class CorrelationProvider(Protocol):
    """Returns a correlation in [-1, 1] between two underlyings."""

    def correlation(self, a: str, b: str) -> float: ...


@dataclass
class ConservativeCorrelationProvider:
    """Assume distinct underlyings are meaningfully correlated until proven otherwise.

    Same underlying -> 1.0. Distinct -> ``default_correlation`` (defaults above the
    clustering threshold, so unknown names cluster together — the safe default).
    """

    default_correlation: float = DEFAULT_ASSUMED_CORRELATION

    def correlation(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        return self.default_correlation


@dataclass
class ReturnsCorrelationProvider:
    """Pearson correlation from per-underlying return series.

    ``returns`` maps an underlying to its (time-ordered) periodic returns. When a
    series is missing or too short, falls back to ``default_correlation`` rather
    than asserting independence — keeping the governor conservative on thin data.
    """

    returns: Mapping[str, list[float]]
    default_correlation: float = DEFAULT_ASSUMED_CORRELATION
    min_observations: int = DEFAULT_MIN_OBSERVATIONS

    def correlation(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        ra = self.returns.get(a)
        rb = self.returns.get(b)
        if not ra or not rb:
            logger.debug(
                "Correlation fallback for %s/%s: missing return series → %.2f",
                a,
                b,
                self.default_correlation,
            )
            return self.default_correlation
        n = min(len(ra), len(rb))
        if n < self.min_observations:
            logger.debug(
                "Correlation fallback for %s/%s: only %d observations (need %d) → %.2f",
                a,
                b,
                n,
                self.min_observations,
                self.default_correlation,
            )
            return self.default_correlation
        value = _pearson(list(ra)[-n:], list(rb)[-n:])
        if value is None:
            logger.debug(
                "Correlation fallback for %s/%s: zero-variance series → %.2f",
                a,
                b,
                self.default_correlation,
            )
            return self.default_correlation
        return value


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation; None if either series has zero variance."""
    n = len(xs)
    if n == 0 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def cluster_underlyings(
    underlyings: Iterable[str],
    provider: CorrelationProvider,
    threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> list[set[str]]:
    """Group underlyings into clusters (connected components) where any pair has
    ``correlation >= threshold``. Each cluster is one *effective opportunity*."""
    names = sorted(set(underlyings))
    parent = {name: name for name in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if provider.correlation(a, b) >= threshold:
                union(a, b)

    clusters: dict[str, set[str]] = {}
    for name in names:
        clusters.setdefault(find(name), set()).add(name)
    return list(clusters.values())


def max_correlation_to_group(
    underlying: str,
    group: Iterable[str],
    provider: CorrelationProvider,
) -> float:
    """Highest correlation between ``underlying`` and any member of ``group``.

    Returns 0.0 for an empty group (nothing to be correlated with)."""
    values = [provider.correlation(underlying, member) for member in group]
    return max(values) if values else 0.0
