"""Time-weighted return vs benchmark — Phase 4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from trading_architect.models.entities import Silo
from trading_architect.services.benchmark import BENCHMARK_SYMBOL
from trading_architect.store.repository import Repository

MIN_HISTORY_DAYS = 90


@dataclass(frozen=True)
class PeriodTwr:
    label: str
    portfolio_twr: float | None
    benchmark_twr: float | None
    sufficient_history: bool
    start_date: date | None
    end_date: date | None


@dataclass(frozen=True)
class TwrReport:
    combined_ytd: PeriodTwr
    combined_t12: PeriodTwr
    stock_options_ytd: PeriodTwr
    stock_options_t12: PeriodTwr


def _chain_link_return(
    series: list[tuple[date, float]],
    cash_flows: dict[date, float],
) -> float | None:
    if len(series) < 2:
        return None
    cumulative = 1.0
    for i in range(1, len(series)):
        _, v0 = series[i - 1]
        d1, v1 = series[i]
        if v0 <= 0:
            continue
        cf = cash_flows.get(d1, 0.0)
        period_r = (v1 - cf) / v0 - 1.0
        cumulative *= 1.0 + period_r
    return cumulative - 1.0


def _benchmark_return(
    repo: Repository,
    start: date,
    end: date,
) -> float | None:
    prices = repo.list_benchmark_prices(BENCHMARK_SYMBOL, since=start, until=end)
    if len(prices) < 2:
        all_prices = repo.list_benchmark_prices(BENCHMARK_SYMBOL)
        in_window = [p for p in all_prices if start <= p.price_date <= end]
        if len(in_window) < 2:
            return None
        prices = in_window
    p0 = prices[0].close_price
    p1 = prices[-1].close_price
    if p0 <= 0:
        return None
    return p1 / p0 - 1.0


def _period_twr(
    repo: Repository,
    *,
    label: str,
    start: date,
    end: date,
    silo: Silo | None,
) -> PeriodTwr:
    full_series = repo.daily_equity_series(silo=silo)
    if not full_series:
        return PeriodTwr(label, None, None, False, None, None)

    series = [(d, v) for d, v in full_series if start <= d <= end]
    if len(series) < 2:
        return PeriodTwr(label, None, None, False, start, end)

    span_days = (series[-1][0] - series[0][0]).days
    sufficient = span_days >= MIN_HISTORY_DAYS
    cash_flows = repo.daily_cash_flows(silo=silo)
    portfolio = _chain_link_return(series, cash_flows)
    benchmark = _benchmark_return(repo, series[0][0], series[-1][0])

    if not sufficient:
        return PeriodTwr(label, portfolio, benchmark, False, series[0][0], series[-1][0])

    return PeriodTwr(label, portfolio, benchmark, True, series[0][0], series[-1][0])


def twr_report(repo: Repository, *, as_of: date | None = None) -> TwrReport:
    """YTD and trailing-12m TWR vs SPY for combined and stock/options silo."""
    ref = as_of or date.today()
    ytd_start = date(ref.year, 1, 1)
    t12_start = ref - timedelta(days=365)

    return TwrReport(
        combined_ytd=_period_twr(
            repo, label="YTD", start=ytd_start, end=ref, silo=None
        ),
        combined_t12=_period_twr(
            repo, label="Trailing 12m", start=t12_start, end=ref, silo=None
        ),
        stock_options_ytd=_period_twr(
            repo,
            label="YTD (stock/options)",
            start=ytd_start,
            end=ref,
            silo=Silo.STOCK_OPTIONS,
        ),
        stock_options_t12=_period_twr(
            repo,
            label="Trailing 12m (stock/options)",
            start=t12_start,
            end=ref,
            silo=Silo.STOCK_OPTIONS,
        ),
    )


def format_twr_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1%}"
