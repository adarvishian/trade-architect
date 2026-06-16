"""Book context — equity, exposure, and governor state for UI and engines (M6)."""

from __future__ import annotations

from dataclasses import dataclass

from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.drawdown import (
    DrawdownMetrics,
    assess_drawdown_state,
    compute_drawdown_pct,
    worst_governor_state,
)
from trading_architect.engines.equity import (
    MarkFallbackStats,
    account_equity_metrics,
    equity_metrics_for_silo,
    mark_fallback_stats_for_book,
)
from trading_architect.engines.formulas import leverage_ratio, portfolio_heat
from trading_architect.engines.marks import MarksProvider, default_marks_provider
from trading_architect.models.entities import Position, Silo, TradeEvent
from trading_architect.store.repository import Repository


@dataclass(frozen=True)
class SiloBookState:
    silo: Silo
    equity: float
    peak_equity: float
    drawdown_pct: float
    governor: DrawdownMetrics
    open_dollar_risk: float
    open_delta_notional: float
    heat: float
    leverage: float
    heat_cap: float
    leverage_cap: float
    open_position_count: int


@dataclass(frozen=True)
class BookContext:
    stock_options: SiloBookState
    futures: SiloBookState
    account_equity: float
    account_peak_equity: float
    account_drawdown_pct: float
    account_governor: DrawdownMetrics
    effective_drawdown_pct: float
    effective_governor: DrawdownMetrics
    mark_fallbacks: MarkFallbackStats

    def drawdown_for_silo(self, silo: Silo) -> float:
        if silo == Silo.STOCK_OPTIONS:
            return self.stock_options.drawdown_pct
        return self.futures.drawdown_pct

    def equity_for_silo(self, silo: Silo) -> float:
        if silo == Silo.STOCK_OPTIONS:
            return self.stock_options.equity
        return self.futures.equity

    def exposure_for_silo(self, silo: Silo, *, repo=None, settings=None):
        from trading_architect.engines.sizing import SiloExposure

        state = self.stock_options if silo == Silo.STOCK_OPTIONS else self.futures
        cash = buying_power = None
        capital_base = None
        capital_base_detail = None
        capital_base_warnings: tuple[str, ...] = ()

        if repo is not None:
            from trading_architect.engines.capital import (
                resolve_capital_base,
                silo_brokerage_liquidity,
            )

            cash, buying_power = silo_brokerage_liquidity(repo, silo)
            if settings is not None:
                base = resolve_capital_base(repo, settings, silo, state.equity)
                capital_base = base.amount
                capital_base_detail = base.detail
                capital_base_warnings = base.warnings

        return SiloExposure(
            silo_equity=state.equity,
            open_dollar_risk=state.open_dollar_risk,
            open_delta_notional=state.open_delta_notional,
            drawdown_pct=self.effective_drawdown_pct,
            peak_equity=state.peak_equity,
            available_cash=cash,
            buying_power=buying_power,
            capital_base=capital_base,
            capital_base_detail=capital_base_detail,
            capital_base_warnings=capital_base_warnings,
        )


def _silo_state(
    silo: Silo,
    events: list[TradeEvent],
    positions: list[Position],
    settings: AppSettings,
    marks_provider: MarksProvider | None = None,
    *,
    live_equity: float | None = None,
    live_peak_equity: float | None = None,
    repo: Repository | None = None,
) -> SiloBookState:
    starting = settings.starting_equity()[silo]
    equity, peak = equity_metrics_for_silo(
        events,
        positions,
        starting,
        silo,
        marks_provider=marks_provider,
        live_equity=live_equity,
    )
    if live_peak_equity is not None:
        peak = live_peak_equity
    if live_equity is not None:
        equity = live_equity

    dd_equity = equity
    dd_peak = peak
    if live_equity is not None and repo is not None:
        from trading_architect.services.cash_events import trading_equity_adjustment

        dd_equity = trading_equity_adjustment(repo, silo, equity)
        if live_peak_equity is not None:
            dd_peak = live_peak_equity

    dd_pct = compute_drawdown_pct(dd_equity, dd_peak)
    governor = assess_drawdown_state(dd_pct, settings.sizing)
    governor = DrawdownMetrics(
        current_equity=equity,
        peak_equity=peak,
        drawdown_pct=dd_pct,
        state=governor.state,
        throttle_multiplier=governor.throttle_multiplier,
        message=governor.message,
    )

    open_positions = [p for p in positions if p.silo == silo and p.status.value == "open"]
    open_risk = sum(p.total_dollar_risk for p in open_positions)
    open_notional = sum(p.current_delta_notional for p in open_positions)
    heat = portfolio_heat(open_risk, equity)
    lev = leverage_ratio(open_notional, equity)

    return SiloBookState(
        silo=silo,
        equity=equity,
        peak_equity=peak,
        drawdown_pct=dd_pct,
        governor=governor,
        open_dollar_risk=open_risk,
        open_delta_notional=open_notional,
        heat=heat,
        leverage=lev,
        heat_cap=settings.sizing.heat_cap,
        leverage_cap=settings.sizing.leverage_cap,
        open_position_count=len(open_positions),
    )


def build_book_context(
    events: list[TradeEvent],
    positions: list[Position],
    settings: AppSettings,
    marks_provider: MarksProvider | None = None,
    *,
    live_stock_options_equity: float | None = None,
    live_futures_equity: float | None = None,
    live_stock_options_peak: float | None = None,
    live_futures_peak: float | None = None,
    repo: Repository | None = None,
) -> BookContext:
    """Aggregate per-silo and account-level book state for dashboards and sizing."""
    marks_provider = marks_provider if marks_provider is not None else default_marks_provider()
    stock = _silo_state(
        Silo.STOCK_OPTIONS,
        events,
        positions,
        settings,
        marks_provider,
        live_equity=live_stock_options_equity,
        live_peak_equity=live_stock_options_peak,
        repo=repo,
    )
    fut = _silo_state(
        Silo.FUTURES,
        events,
        positions,
        settings,
        marks_provider,
        live_equity=live_futures_equity,
        live_peak_equity=live_futures_peak,
        repo=repo,
    )

    account_equity = stock.equity + fut.equity
    account_peak = account_equity_metrics(
        events, positions, settings.starting_equity(), marks_provider
    )[1]
    account_dd = compute_drawdown_pct(account_equity, account_peak)
    account_gov = assess_drawdown_state(account_dd, settings.sizing)
    account_gov = DrawdownMetrics(
        current_equity=account_equity,
        peak_equity=account_peak,
        drawdown_pct=account_dd,
        state=account_gov.state,
        throttle_multiplier=account_gov.throttle_multiplier,
        message=account_gov.message,
    )

    effective_dd = max(stock.drawdown_pct, fut.drawdown_pct, account_dd)
    effective_gov = assess_drawdown_state(effective_dd, settings.sizing)
    effective_gov = DrawdownMetrics(
        current_equity=account_equity,
        peak_equity=account_peak,
        drawdown_pct=effective_dd,
        state=worst_governor_state([stock.governor.state, fut.governor.state, account_gov.state]),
        throttle_multiplier=effective_gov.throttle_multiplier,
        message=effective_gov.message,
    )

    mark_fallbacks = mark_fallback_stats_for_book(events, positions, marks_provider)

    return BookContext(
        stock_options=stock,
        futures=fut,
        account_equity=account_equity,
        account_peak_equity=account_peak,
        account_drawdown_pct=account_dd,
        account_governor=account_gov,
        effective_drawdown_pct=effective_dd,
        effective_governor=effective_gov,
        mark_fallbacks=mark_fallbacks,
    )
