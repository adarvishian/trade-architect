"""R-distribution analytics — PRD FR-6.4, Appendix A.7."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class BootstrapCI:
    """Point estimate with bootstrap confidence interval (PRD A.7)."""

    point: float
    lower: float
    upper: float
    n_samples: int
    ci_level: float


@dataclass(frozen=True)
class RDistributionStats:
    count: int
    expectancy: float
    mean_r: float
    std_r: float
    median_r: float
    skew: float
    win_rate: float
    p05_r: float
    p95_r: float
    optimal_f: float
    kelly_quarter_f: float


def compute_r_distribution(r_values: list[float]) -> RDistributionStats | None:
    """Summarize realized R-multiples for a silo/epoch segment."""
    clean = _clean_r_values(r_values)
    if not clean:
        return None

    arr = np.array(clean, dtype=float)
    win_rate = float(np.mean(arr > 0))
    optimal_f = _estimate_optimal_f(arr)
    skew_val = _compute_skew(arr)

    return RDistributionStats(
        count=len(arr),
        expectancy=float(np.mean(arr)),
        mean_r=float(np.mean(arr)),
        std_r=float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        median_r=float(np.median(arr)),
        skew=skew_val,
        win_rate=win_rate,
        p05_r=float(np.percentile(arr, 5)),
        p95_r=float(np.percentile(arr, 95)),
        optimal_f=optimal_f,
        kelly_quarter_f=optimal_f * 0.25,
    )


def _clean_r_values(r_values: list[float]) -> list[float]:
    return [r for r in r_values if r is not None and np.isfinite(r)]


def _compute_skew(arr: np.ndarray) -> float:
    """Sample skew; returns 0 when variance is degenerate (avoids scipy precision warnings)."""
    if len(arr) <= 2 or len(np.unique(arr)) < 2:
        return 0.0
    return float(stats.skew(arr))


def bootstrap_resample_stats(
    r_values: list[float],
    *,
    n_bootstrap: int = 500,
    ci: float = 0.05,
    seed: int = 42,
) -> tuple[BootstrapCI | None, BootstrapCI | None]:
    """Bootstrap CIs for expectancy and optimal-f (PRD FR-4.1, A.7)."""
    clean = _clean_r_values(r_values)
    if len(clean) < 5:
        return None, None

    rng = np.random.default_rng(seed)
    arr = np.array(clean, dtype=float)
    expectancy_samples: list[float] = []
    optimal_f_samples: list[float] = []
    for _ in range(n_bootstrap):
        sample = rng.choice(arr, size=len(arr), replace=True)
        expectancy_samples.append(float(np.mean(sample)))
        optimal_f_samples.append(_estimate_optimal_f(sample))

    exp_arr = np.array(expectancy_samples, dtype=float)
    f_arr = np.array(optimal_f_samples, dtype=float)
    lower_pct = ci * 100
    upper_pct = (1.0 - ci) * 100

    expectancy_ci = BootstrapCI(
        point=float(np.mean(arr)),
        lower=float(np.percentile(exp_arr, lower_pct)),
        upper=float(np.percentile(exp_arr, upper_pct)),
        n_samples=len(clean),
        ci_level=ci,
    )
    optimal_f_ci = BootstrapCI(
        point=_estimate_optimal_f(arr),
        lower=float(np.percentile(f_arr, lower_pct)),
        upper=float(np.percentile(f_arr, upper_pct)),
        n_samples=len(clean),
        ci_level=ci,
    )
    return expectancy_ci, optimal_f_ci


def bootstrap_optimal_f_lower(
    r_values: list[float],
    *,
    n_bootstrap: int = 500,
    ci: float = 0.05,
    seed: int = 42,
) -> float | None:
    """Lower confidence bound on optimal-f via bootstrap (PRD A.7)."""
    _, optimal_f_ci = bootstrap_resample_stats(
        r_values,
        n_bootstrap=n_bootstrap,
        ci=ci,
        seed=seed,
    )
    return optimal_f_ci.lower if optimal_f_ci else None


def _estimate_optimal_f(r_values: np.ndarray) -> float:
    """Growth-optimal f from R-multiples via grid search (Appendix A.7)."""
    if len(r_values) == 0:
        return 0.0

    grid = np.linspace(0.005, 0.50, 200)
    best_f = 0.0
    best_growth = 1.0

    for f in grid:
        terms = 1.0 + f * r_values
        if np.any(terms <= 0):
            continue
        growth = float(np.exp(np.mean(np.log(terms))))
        if growth > best_growth:
            best_growth = growth
            best_f = float(f)

    return best_f
