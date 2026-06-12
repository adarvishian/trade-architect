"""Dashboard four-block layout — Phase 3 surface."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from ui_helpers import cap_status_pct, silo_book_row

from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.adaptive_risk import RiskAction, build_risk_review
from trading_architect.engines.book_context import BookContext
from trading_architect.engines.capital import (
    deployable_capital,
    monthly_net_cashflow,
    project_equity,
)
from trading_architect.engines.evaluation import CounterfactualRule, evaluate_alpha_left
from trading_architect.engines.marks import default_marks_provider
from trading_architect.ingestion.robinhood_fetch import stock_account_breakdown
from trading_architect.models.entities import Silo
from trading_architect.services.adherence import adherence_summary
from trading_architect.services.book_metrics import heat_utilization
from trading_architect.services.cash_events import pending_cash_events
from trading_architect.services.current_state import account_cards, current_positions
from trading_architect.services.dashboard_metrics import (
    expiry_runway_premium,
    options_theta_day,
    position_risk_rows,
    standard_dollar_risk,
    top_cluster_stress,
    top_concentrations,
)
from trading_architect.services.earnings import earnings_flags_for_positions
from trading_architect.services.exit_efficiency import exit_efficiency_report
from trading_architect.services.position_stops import has_stop
from trading_architect.services.stop_slippage import (
    portfolio_slippage_adjusted_heat,
    slippage_calibration,
)
from trading_architect.services.twr import format_twr_pct, twr_report
from trading_architect.store.repository import Repository


def render_deposit_prompts(repo: Repository) -> None:
    pending = pending_cash_events(repo)
    if not pending:
        return
    accounts = {a.id: a.label for a in repo.list_accounts()}
    for event in pending:
        label = accounts.get(event.account_id, f"account #{event.account_id}")
        st.warning(
            f"**{label}** cash changed ${event.prior_cash:,.0f} → ${event.new_cash:,.0f} "
            f"({event.delta:+,.0f}). Classify this jump:"
        )
        c1, c2, c3 = st.columns(3)
        for col, classification in zip(
            (c1, c2, c3),
            ("deposit", "withdrawal", "market_move"),
            strict=True,
        ):
            btn_label = classification.replace("_", " ").title()
            if col.button(btn_label, key=f"cash_evt_{event.id}_{classification}"):
                from trading_architect.services.cash_events import classify_cash_event

                classify_cash_event(repo, event.id, classification)
                st.rerun()


def render_dashboard(
    repo: Repository,
    settings: AppSettings,
    events: list,
    book: BookContext,
) -> None:
    st.header("Dashboard")
    marks_provider = default_marks_provider()
    holdings = current_positions(repo, marks_provider)
    cards = account_cards(repo)
    deploy = deployable_capital(repo, settings)
    sync_results = {r.account_label: r for r in st.session_state.get("sync_results", [])}

    as_of_times = [c["as_of"] for c in cards if c.get("as_of")]
    book_as_of = max(as_of_times) if as_of_times else None
    as_of_text = (
        book_as_of.strftime("%Y-%m-%d %H:%M UTC") if book_as_of else "—"
    )

    render_deposit_prompts(repo)

    _render_twr_card(repo)

    # Block 1 — Where am I
    st.subheader("Where am I")
    w1, w2, w3 = st.columns(3)
    w1.metric("Total capital", f"${deploy.total_capital:,.0f}")
    w2.metric("Stock/options silo", f"${book.stock_options.equity:,.0f}")
    w3.metric("Futures silo", f"${book.futures.equity:,.0f}")
    st.caption(f"As of {as_of_text}")

    if cards:
        acct_rows = []
        for card in cards:
            sync = sync_results.get(card["label"])
            status = sync.status if sync else "stale"
            acct_rows.append(
                {
                    "account": card["label"],
                    "institution": card["institution"] or card["kind"],
                    "value": card["equity_value"],
                    "cash": card["cash"],
                    "as_of": card["as_of"].strftime("%Y-%m-%d %H:%M")
                    if card["as_of"]
                    else "—",
                    "sync": status,
                }
            )
        st.dataframe(pd.DataFrame(acct_rows), use_container_width=True, hide_index=True)

    # Block 2 — Where is risk
    st.subheader("Where is risk")
    cfg = settings.sizing
    r1, r2, r3, r4 = st.columns(4)
    so = book.stock_options
    r1.metric("Open heat", f"${so.open_dollar_risk:,.0f}", f"{so.heat:.0%} of equity")
    cal = slippage_calibration(repo)
    if holdings:
        raw_stop, adj_stop = portfolio_slippage_adjusted_heat(holdings, repo, cal)
        if cal.status == "calibrated" and adj_stop > raw_stop:
            adj_heat = adj_stop / so.equity if so.equity > 0 else 0.0
            r1.caption(
                f"Heat (slippage-adj): ${adj_stop:,.0f} ({adj_heat:.0%} of cap)"
            )
        elif cal.status == "calibrating":
            r1.caption("Slippage pad: calibrating (<10 exit observations)")
    r2.metric("Heat cap", f"{cfg.heat_cap:.0%}", cap_status_pct(so.heat, cfg.heat_cap))
    r3.metric("Drawdown", f"{book.effective_drawdown_pct:.1%}", book.effective_governor.state.value)
    r4.metric("Governor", book.effective_governor.state.value.replace("_", " "))

    theta = options_theta_day(holdings, marks_provider) if holdings else 0.0
    runway = expiry_runway_premium(holdings) if holdings else None
    t1, t2 = st.columns(2)
    if theta is None:
        t1.metric("Options theta bleed", "—")
        t1.caption("⚠ Marks unavailable — theta not shown")
    else:
        t1.metric("Options theta bleed", f"${theta:,.0f}/day")
    if runway and runway.total > 0:
        t2.caption(
            f"Premium at risk by DTE: <30d ${runway.under_30:,.0f} · "
            f"<60d ${runway.under_60:,.0f} · <90d ${runway.under_90:,.0f} · "
            f"≥90d ${runway.gte_90:,.0f}"
        )

    concentrations = top_concentrations(holdings)
    if concentrations:
        st.caption(
            "Top concentrations: "
            + " · ".join(f"{u} ${n:,.0f}" for u, n in concentrations)
        )

    stress = top_cluster_stress(
        holdings,
        repo,
        equity=book.stock_options.equity,
        stress_pct=settings.cluster_stress_pct,
    ) if holdings else None
    if stress:
        gap_pct_label = abs(settings.cluster_stress_pct) * 100
        st.caption(
            f"Top cluster **{stress.cluster_label}** = {stress.cluster_pct:.0%} of book · "
            f"{gap_pct_label:.0f}% gap ≈ ${stress.stress_gap_dollars:,.0f} "
            f"({stress.stress_gap_equity_pct:.1%} of equity)"
        )

    if holdings:
        flags = earnings_flags_for_positions(repo, holdings)
        upcoming = [f for f in flags if f.earnings_date and f.days_until is not None and f.days_until <= 14]
        if upcoming:
            for flag in upcoming:
                legs = f", {flag.option_legs_held} calls/puts held" if flag.option_legs_held else ""
                src = " (manual)" if flag.source == "manual" else ""
                st.caption(
                    f"📅 **{flag.underlying}** earnings in {flag.days_until}d{legs}{src}"
                )
        elif any(f.degraded for f in flags):
            st.caption("Earnings dates: degraded — set manual dates in Settings.")

    pending_unclassified = [
        e for e in pending_cash_events(repo)
    ]
    if pending_unclassified:
        st.warning(
            f"**{len(pending_unclassified)} unclassified cash event(s)** — "
            "deposits/withdrawals may distort TWR until classified."
        )

    if holdings:
        risk_rows = position_risk_rows(holdings, repo)
        for row in risk_rows:
            if row["no_stop"]:
                row["flag"] = "⚠ no stop"
            else:
                row["flag"] = ""
        st.dataframe(
            pd.DataFrame(risk_rows),
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("**Open positions** (stop editing)")
        _render_positions_table(repo, settings, book, holdings)

    st.subheader("Silo exposure vs caps")
    summary = pd.DataFrame([silo_book_row(book.stock_options), silo_book_row(book.futures)])
    st.dataframe(
        summary[
            [
                "silo",
                "equity",
                "drawdown",
                "governor",
                "heat",
                "heat_cap",
                "heat_status",
                "leverage",
                "leverage_cap",
                "lev_status",
                "open_positions",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    fb = book.mark_fallbacks
    if fb.legs_at_cost_basis or fb.delta_iv_fallbacks:
        st.warning(
            f"Mark fallbacks active: **{fb.legs_at_cost_basis}** leg(s) at cost basis, "
            f"**{fb.delta_iv_fallbacks}** delta IV fallback(s)."
        )

    # Block 3 — What can I allocate
    st.subheader("What can I allocate")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Deployable", f"${deploy.deployable_total:,.0f}")
    d2.metric("Brokerage cash", f"${deploy.brokerage_cash:,.0f}")
    d3.metric("Transferable", f"${deploy.transferable_cash:,.0f}")
    d4.metric("Reserve held", f"${deploy.reserve_held:,.0f}")
    st.caption(
        f"Next month net **${deploy.monthly_net_cashflow:,.0f}** "
        f"(income ${settings.monthly_income_after_tax:,.0f} − expenses ${settings.monthly_expenses:,.0f}). "
        f"Reserve = {settings.cash_reserve_months}× expenses. As of {as_of_text}."
    )

    st.subheader("Equity projection")
    return_pct = st.number_input(
        "Annual return assumption (%)",
        value=0.0,
        min_value=-50.0,
        max_value=100.0,
        step=1.0,
        key="proj_return_pct",
        help="User-set scenario only — not estimated from trade history.",
    )
    contrib = monthly_net_cashflow(settings)
    base_equity = book.account_equity
    for months in (6, 12):
        series = project_equity(base_equity, contrib, return_pct / 100.0, months)
        st.metric(f"{months}-month projected equity", f"${series[-1]:,.0f}")

    # Block 4 — What per trade
    st.subheader("What per trade")
    std_risk = standard_dollar_risk(
        book.stock_options.equity,
        settings.sizing.base_risk_f,
        settings.sizing.kelly_fraction,
    )
    p1, p2, p3 = st.columns(3)
    p1.metric("Standard dollar risk (stock/options)", f"${std_risk:,.0f}")
    heat_util = heat_utilization(repo, silo=Silo.STOCK_OPTIONS, heat_cap=cfg.heat_cap)
    if heat_util is not None:
        p2.metric("Heat utilization (90d avg)", f"{heat_util:.0%}")
    else:
        p2.metric("Heat utilization (90d avg)", "accumulating…")

    if st.button("Size a trade →", type="primary", key="dash_to_size"):
        st.session_state["nav_page"] = "Size a Trade"
        st.rerun()

    _render_edge_card(events, settings, book)
    _render_sizing_efficiency_card(events, settings)
    _render_adherence_card(repo)
    _render_exit_efficiency_card(repo)


def _render_twr_card(repo: Repository) -> None:
    report = twr_report(repo)
    st.markdown("**Performance vs SPY (TWR, net of deposits/withdrawals)**")
    st.caption("SPY benchmark uses price return only (excl. dividends).")

    def _row(period) -> None:
        if not period.sufficient_history:
            st.caption(f"{period.label}: insufficient history (<3 months of snapshots)")
            return
        c1, c2, c3 = st.columns(3)
        c1.metric(period.label, format_twr_pct(period.portfolio_twr))
        c2.metric("SPY (excl. div.)", format_twr_pct(period.benchmark_twr))
        if period.portfolio_twr is not None and period.benchmark_twr is not None:
            alpha = period.portfolio_twr - period.benchmark_twr
            c3.metric("vs SPY", format_twr_pct(alpha))
        else:
            c3.metric("vs SPY", "—")

    _row(report.combined_ytd)
    _row(report.combined_t12)


def _render_exit_efficiency_card(repo: Repository) -> None:
    report = exit_efficiency_report(repo, limit=20)
    st.markdown("**Exit efficiency (MFE capture, evaluation only)**")
    if report.trade_count == 0:
        st.caption("No closed stock/futures round-trips with mark history yet.")
        return
    if report.median_mfe_capture_pct is not None:
        st.metric(
            f"Median MFE captured (last {report.trade_count} closed)",
            f"{report.median_mfe_capture_pct:.0%}",
        )
    with st.expander("Exit efficiency drill-down"):
        rows = []
        for row in report.rows:
            rows.append(
                {
                    "underlying": row.underlying,
                    "closed": row.closed_at.strftime("%Y-%m-%d"),
                    "exit_r": row.exit_r,
                    "max_r": row.max_r_reached,
                    "mfe_capture": (
                        f"{row.mfe_capture_pct:.0%}" if row.mfe_capture_pct is not None else "—"
                    ),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_positions_table(
    repo: Repository,
    settings: AppSettings,
    book: BookContext,
    holdings: list,
) -> None:
    overrides = {(o.symbol, o.silo.value): o for o in repo.list_position_overrides()}
    rows = []
    for p in holdings:
        stop_set = has_stop(p, repo)
        rows.append(
            {
                "underlying": p.underlying,
                "accounts": stock_account_breakdown(p),
                "open_r": round(p.open_r, 2) if p.open_r is not None else None,
                "no_stop": "⚠" if not stop_set else "",
                "total_risk": p.total_dollar_risk,
                "notional": p.current_delta_notional,
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    stop_rows = []
    for p in holdings:
        ov = overrides.get((p.underlying, p.silo.value))
        stop_rows.append(
            {
                "underlying": p.underlying,
                "silo": p.silo.value,
                "initial_stop": ov.initial_stop if ov else None,
                "current_stop": ov.current_stop if ov else None,
            }
        )
    if stop_rows:
        edited = st.data_editor(
            pd.DataFrame(stop_rows),
            num_rows="fixed",
            column_config={
                "initial_stop": st.column_config.NumberColumn("Initial stop", format="%.2f"),
                "current_stop": st.column_config.NumberColumn("Current stop", format="%.2f"),
            },
            key="dashboard_stops_editor",
        )
        if st.button("Save stops", key="dashboard_save_stops"):
            for _, row in edited.iterrows():
                init = row["initial_stop"]
                curr = row["current_stop"]
                if init is not None or curr is not None:
                    repo.upsert_position_override(
                        symbol=str(row["underlying"]),
                        silo=Silo(row["silo"]),
                        initial_stop=float(init) if init is not None and not pd.isna(init) else None,
                        current_stop=float(curr) if curr is not None and not pd.isna(curr) else None,
                    )
            st.success("Stops saved.")
            st.rerun()


def _render_edge_card(events: list, settings: AppSettings, book: BookContext) -> None:
    if not events:
        return
    report = build_risk_review(
        events,
        config=settings.adaptive_risk,
        sizing=settings.sizing,
        drawdown_pct=book.effective_drawdown_pct,
    )
    if not report.recommendations:
        return
    st.markdown("**Kelly step-up / risk appetite**")
    for rec in report.recommendations:
        action_colors = {
            RiskAction.STEP_UP_KELLY: "success",
            RiskAction.STEP_UP_BASE_F: "success",
            RiskAction.DE_RISK: "error",
            RiskAction.INSUFFICIENT_DATA: "warning",
            RiskAction.HOLD: "info",
        }
        msg_fn = getattr(st, action_colors.get(rec.action, "info"))
        trades_needed = rec.edge.trades_needed_for_step_up
        extra = f" · {trades_needed} more trades needed" if trades_needed > 0 else ""
        msg_fn(f"**{rec.silo.value}**: {rec.action.value.replace('_', ' ')} — {rec.narrative}{extra}")
        with st.expander(f"Edge stats — {rec.silo.value}"):
            st.text(rec.statistics)


def _render_sizing_efficiency_card(events: list, settings: AppSettings) -> None:
    if not events:
        st.caption("Sizing efficiency: import history for evaluation.")
        return
    report = evaluate_alpha_left(events, starting_equity=settings.starting_equity())
    alpha_total = 0.0
    for seg in report.segments:
        if seg.rule != CounterfactualRule.KELLY_QUARTER:
            continue
        alpha_total += seg.alpha_left_on_table
    st.markdown("**Sizing efficiency (actual vs ¼ Kelly, ledger)**")
    st.metric("Alpha left on table (¼ Kelly)", f"${alpha_total:,.0f}")
    with st.expander("Sizing efficiency drill-down"):
        rows = []
        for seg in report.segments:
            if seg.rule != CounterfactualRule.KELLY_QUARTER:
                continue
            rows.append(
                {
                    "silo": seg.silo.value,
                    "epoch": seg.epoch_id,
                    "trades": seg.trade_count,
                    "actual_pnl": seg.total_actual_pnl,
                    "cf_pnl": seg.total_counterfactual_pnl,
                    "alpha_left": seg.alpha_left_on_table,
                }
            )
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No closed trades for evaluation yet.")


def _render_adherence_card(repo: Repository) -> None:
    summary = adherence_summary(repo)
    st.markdown("**Adherence (recommended vs taken, trailing 90d)**")
    if summary.match_count == 0:
        st.caption("No matched recommendations yet — run Size a Trade to record recommendations.")
        return
    pct = summary.adherence_pct
    p1, p2 = st.columns(2)
    p1.metric("Adherence", f"{pct:.0%}" if pct is not None else "—")
    p2.metric("Est. gap from under-sizing", f"${summary.gap_cost_total:,.0f}")
