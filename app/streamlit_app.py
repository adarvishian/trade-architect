"""Trading Architect — Streamlit UI (M6 polish)."""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from ui_helpers import cap_status_pct, render_governor_sidebar, show_ui_error, silo_book_row

import trading_architect  # noqa: F401 — loads .env on import
from trading_architect.bootstrap import (
    build_app_book_context,
    create_repository,
    fetch_schwab_portfolio_snapshot,
    import_and_assemble,
    import_robinhood_fetch,
    import_schwab_fetch,
    sync_schwab_live_equity,
)
from trading_architect.config.env import (
    load_env,
    rh_credentials_configured,
    schwab_credentials_configured,
)
from trading_architect.config.user_settings import (
    AppSettings,
    app_settings_from_json,
    app_settings_to_json,
    diff_app_settings,
)
from trading_architect.ingestion.robinhood_fetch import (
    fetch_portfolio_snapshot,
    format_holding_label,
    holding_breakdown,
    robin_stocks_available,
    stock_account_breakdown,
)
from trading_architect.ingestion.schwab_accounts import list_accounts
from trading_architect.ingestion.schwab_auth import schwab_connection_status, schwab_py_available
from trading_architect.models.entities import Silo

load_env()

PAGES = [
    "Dashboard",
    "Import Data",
    "Size a Trade",
    "Option Selector",
    "Current Positions",
    "Alpha Left on Table",
    "Edge & Risk Review",
    "Review Queue",
    "Settings",
]


@st.cache_resource
def get_repository():
    return create_repository()


def load_session_data():
    repo = get_repository()
    if "app_settings" not in st.session_state:
        st.session_state.app_settings = repo.load_app_settings()
    settings: AppSettings = st.session_state.app_settings
    events = repo.list_events()
    positions = repo.list_positions()
    book = build_app_book_context(events, positions, settings)
    return repo, settings, events, positions, book


def save_settings(repo, new_settings: AppSettings) -> None:
    new_settings.adaptive_risk.current_base_f = new_settings.sizing.base_risk_f
    new_settings.adaptive_risk.current_kelly_fraction = new_settings.sizing.kelly_fraction
    old = st.session_state.get("app_settings", repo.load_app_settings())
    overrides = diff_app_settings(old, new_settings)
    repo.save_app_settings(new_settings, overrides=overrides)
    st.session_state.app_settings = new_settings


st.set_page_config(page_title="Trading Architect", layout="wide", initial_sidebar_state="expanded")

repo, settings, events, positions, book = load_session_data()

st.sidebar.title("Trading Architect")
page = st.sidebar.radio("Navigate", PAGES, index=0)
render_governor_sidebar(book)

st.title("Trading Architect")
st.caption(
    "Risk-adjusted position sizing & trade evaluation — import CSVs, size trades, review edge."
)

if page == "Dashboard":
    st.header("Dashboard")
    if not events:
        st.info("Import broker CSVs to populate your books. Start under **Import Data**.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trade events", repo.event_count())
        c2.metric("Positions", repo.position_count())
        c3.metric("Account equity", f"${book.account_equity:,.0f}")
        c4.metric("Effective drawdown", f"{book.effective_drawdown_pct:.1%}")

        st.subheader("Book exposure vs caps")
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
                f"Mark fallbacks active: **{fb.legs_at_cost_basis}** open leg(s) priced at cost basis, "
                f"**{fb.delta_iv_fallbacks}** delta estimate(s) using default IV (20%). "
                "MTM equity may understate drawdown until live marks are available."
            )
            if fb.missing_mark_symbols:
                st.caption(f"Missing marks: {', '.join(fb.missing_mark_symbols)}")

        review_count = len(repo.list_review_queue())
        if review_count:
            st.warning(f"{review_count} row(s) in the review queue — see **Review Queue**.")

        imports = repo.list_import_log(limit=5)
        if imports:
            st.subheader("Recent imports")
            st.dataframe(pd.DataFrame(imports), use_container_width=True, hide_index=True)

elif page == "Import Data":
    st.header("Import Data")
    csv_tab, roth_tab, schwab_tab = st.tabs(
        ["CSV upload", "Robinhood API (Roth IRA)", "Schwab API"]
    )

    with csv_tab:
        st.write("Drop CSV exports from Robinhood, Schwab/thinkorswim, or Tradovate.")
        broker = st.selectbox("Broker", ["robinhood", "schwab", "tradovate"], key="csv_broker")
        account = st.text_input(
            "Account label",
            "",
            key="csv_account",
            help="e.g. robinhood-individual, schwab-tos, tradovate",
        )
        uploaded = st.file_uploader("CSV file", type=["csv"], key="csv_upload")

        if uploaded and st.button("Import CSV", type="primary", key="csv_import_btn"):
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                tmp.write(uploaded.getvalue())
                tmp_path = Path(tmp.name)

            result = import_and_assemble(
                tmp_path, broker=broker, account=account or None, repo=repo
            )
            st.cache_resource.clear()
            st.success(
                f"Imported **{result.imported}** events "
                f"({result.skipped_duplicates} duplicates skipped, "
                f"{result.review_queue} sent to review queue)."
            )
            st.metric("Positions assembled", repo.position_count())

            if broker == "tradovate":
                from trading_architect.engines.trade_stats import (
                    format_duration,
                    summarize_closed_positions,
                )
                from trading_architect.models.entities import Silo

                positions = repo.list_positions()
                stats = summarize_closed_positions(positions, silo=Silo.FUTURES)
                if stats:
                    st.subheader("Futures performance summary")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Gross P/L", f"${stats.gross_pnl:,.2f}")
                    c2.metric("Trades", stats.trade_count)
                    c3.metric("% Profitable", f"{stats.win_rate:.2%}")
                    c4.metric("Expectancy", f"${stats.expectancy:,.2f}")
                    st.caption(
                        f"Wins: {stats.win_count} (${stats.total_profit:,.2f}) · "
                        f"Losses: {stats.loss_count} (${stats.total_loss:,.2f}) · "
                        f"Avg time: {format_duration(stats.avg_trade_duration)} · "
                        f"Longest: {format_duration(stats.longest_trade_duration)}"
                    )

            st.rerun()

    with roth_tab:
        st.write(
            "Robinhood Roth IRA has no in-app CSV export. "
            "Pull fills directly from Robinhood's API and import into the store."
        )

        if not robin_stocks_available():
            st.warning(
                "Install the optional dependency first: "
                '`pip install robin-stocks` or `pip install -e ".[robinhood]"`'
            )
        else:
            st.success("robin-stocks is installed.")

        env_user = os.environ.get("RH_USERNAME", "")
        using_env = rh_credentials_configured()

        if using_env:
            st.info(f"Using credentials from environment (RH_USERNAME={env_user}).")
        else:
            st.caption("Credentials are used for this session only — never stored in the database.")

        username = st.text_input(
            "Robinhood email",
            value=env_user,
            disabled=using_env,
            key="rh_username",
        )
        password = st.text_input(
            "Robinhood password",
            type="password",
            value="" if not using_env else "********",
            disabled=using_env,
            key="rh_password",
        )
        mfa_code = st.text_input(
            "2FA code (if prompted)",
            help="Enter only when Robinhood requires MFA for this login.",
            key="rh_mfa",
        )

        with st.form("robinhood_fetch_form"):
            rh_account = st.selectbox(
                "Account",
                ["robinhood-roth", "robinhood-ira", "robinhood-individual"],
                help="Maps to your Robinhood account type after login.",
            )
            save_audit_csv = st.checkbox("Save audit CSV to data/raw/", value=True)
            submitted = st.form_submit_button("Fetch & Import", type="primary")

        if submitted:
            if not robin_stocks_available():
                st.error("Install robin-stocks before fetching.")
            elif not using_env and (not username or not password):
                st.error(
                    "Enter Robinhood credentials or set RH_USERNAME / RH_PASSWORD in your environment."
                )
            else:
                with st.spinner(f"Logging in and fetching {rh_account}..."):
                    try:
                        result = import_robinhood_fetch(
                            account=rh_account,
                            username=None if using_env else username,
                            password=None if using_env else password,
                            mfa_code=mfa_code or None,
                            save_csv=save_audit_csv,
                            repo=repo,
                        )
                        st.cache_resource.clear()
                        st.success(
                            f"Imported **{result.imported}** events from Robinhood API "
                            f"({result.skipped_duplicates} duplicates skipped)."
                        )
                        if save_audit_csv:
                            st.caption("Audit CSV saved under `data/raw/`.")
                        st.metric("Positions assembled", repo.position_count())
                        st.rerun()
                    except ImportError:
                        st.error("robin-stocks is not installed.")
                    except Exception as exc:
                        show_ui_error(exc, context="Robinhood fetch & import")

        st.divider()
        st.subheader("Portfolio snapshot")
        st.caption(
            "Pull cash/cash-equivalent balances and open holdings from Roth + individual accounts. "
            "Cash totals feed starting-equity sizing; holdings show sum-of-parts by account."
        )

        with st.form("robinhood_portfolio_form"):
            portfolio_accounts = st.multiselect(
                "Accounts to include",
                ["robinhood-roth", "robinhood-individual", "robinhood-ira"],
                default=["robinhood-roth", "robinhood-individual"],
            )
            portfolio_submitted = st.form_submit_button(
                "Fetch balances & holdings", type="secondary"
            )

        if portfolio_submitted:
            if not robin_stocks_available():
                st.error("Install robin-stocks before fetching.")
            elif not using_env and (not username or not password):
                st.error("Enter Robinhood credentials or set RH_USERNAME / RH_PASSWORD.")
            else:
                with st.spinner("Fetching Robinhood portfolio..."):
                    try:
                        snapshot = fetch_portfolio_snapshot(
                            accounts=portfolio_accounts or None,
                            username=None if using_env else username,
                            password=None if using_env else password,
                            mfa_code=mfa_code or None,
                        )
                        st.session_state["rh_portfolio_snapshot"] = snapshot
                    except Exception as exc:
                        show_ui_error(exc, context="Robinhood portfolio fetch")

        snapshot = st.session_state.get("rh_portfolio_snapshot")
        if snapshot:
            bal_rows = [
                {
                    "account": b.account.replace("robinhood-", ""),
                    "cash": b.cash,
                    "cash_equivalents": b.cash_equivalents,
                    "cash_and_equivalents": b.cash_and_equivalents,
                    "portfolio_equity": b.portfolio_equity,
                    "buying_power": b.buying_power,
                }
                for b in snapshot.accounts
            ]
            st.dataframe(pd.DataFrame(bal_rows), use_container_width=True, hide_index=True)
            c1, c2 = st.columns(2)
            c1.metric("Total cash & equivalents", f"${snapshot.total_cash_and_equivalents:,.2f}")
            c2.metric("Total portfolio equity", f"${snapshot.total_portfolio_equity:,.2f}")

            if snapshot.holdings:
                hold_rows = []
                for h in snapshot.holdings:
                    hold_rows.append(
                        {
                            "position": format_holding_label(h),
                            "type": h.asset_type.value,
                            "qty": h.total_quantity,
                            "avg_cost": h.average_cost,
                            "market_value": h.market_value or None,
                            "by_account": holding_breakdown(h),
                        }
                    )
                st.dataframe(pd.DataFrame(hold_rows), use_container_width=True, hide_index=True)

            if st.button("Use total portfolio equity as stock/options starting equity"):
                settings.starting_equity_stock_options = snapshot.total_portfolio_equity
                repo.save_app_settings(settings)
                st.session_state.app_settings = settings
                st.success(
                    f"Starting equity (stock/options) set to ${snapshot.total_portfolio_equity:,.2f}"
                )

    with schwab_tab:
        st.write(
            "Schwab/thinkorswim via the Trader API: portfolio snapshot (MTM equity) and trade import. "
            "One-time OAuth: `ta fetch-schwab --login`."
        )
        schwab_status = schwab_connection_status()
        if not schwab_status["installed"]:
            st.warning('Install schwab-py: `pip install -e ".[schwab]"`')
        elif not schwab_status["configured"]:
            st.warning("Set SCHWAB_API_KEY and SCHWAB_APP_SECRET in .env")
        elif not schwab_status["token"]:
            st.warning(str(schwab_status["message"]))
        else:
            st.success(str(schwab_status["message"]))
            if schwab_status.get("days_left") is not None:
                st.caption(f"Refresh token: {schwab_status['days_left']} day(s) remaining")

        st.divider()
        st.subheader("Portfolio snapshot")
        st.caption(
            "Balances and open positions from Schwab REST. "
            "MTM equity uses currentBalances.liquidationValue for the drawdown governor."
        )
        mapped_labels = sorted(settings.schwab_account_hashes.keys())
        with st.form("schwab_portfolio_form"):
            schwab_portfolio_labels = st.multiselect(
                "Accounts to include",
                mapped_labels or ["schwab-tos"],
                default=mapped_labels or ["schwab-tos"],
            )
            portfolio_submitted = st.form_submit_button(
                "Fetch balances & holdings",
                type="secondary",
            )

        if portfolio_submitted:
            if not schwab_py_available():
                st.error("Install schwab-py before fetching.")
            elif not schwab_credentials_configured() or not schwab_status["token"]:
                st.error("Configure Schwab credentials and complete OAuth login first.")
            else:
                with st.spinner("Fetching Schwab portfolio..."):
                    try:
                        from trading_architect.ingestion.schwab_auth import SchwabAuthExpired

                        snapshot = fetch_schwab_portfolio_snapshot(
                            labels=schwab_portfolio_labels or None,
                            repo=repo,
                        )
                        st.session_state["schwab_portfolio_snapshot"] = snapshot
                        st.session_state.app_settings = repo.load_app_settings()
                        settings = st.session_state.app_settings
                    except SchwabAuthExpired as exc:
                        st.error(str(exc))
                    except Exception as exc:
                        show_ui_error(exc, context="Schwab portfolio fetch")

        schwab_snapshot = st.session_state.get("schwab_portfolio_snapshot")
        if schwab_snapshot:
            bal_rows = [
                {
                    "account": b.account,
                    "cash": b.cash,
                    "cash_equivalents": b.cash_equivalents,
                    "cash_and_equivalents": b.cash_and_equivalents,
                    "portfolio_equity": b.portfolio_equity,
                    "buying_power": b.buying_power,
                }
                for b in schwab_snapshot.accounts
            ]
            st.dataframe(pd.DataFrame(bal_rows), use_container_width=True, hide_index=True)
            c1, c2 = st.columns(2)
            c1.metric(
                "Total cash & equivalents", f"${schwab_snapshot.total_cash_and_equivalents:,.2f}"
            )
            c2.metric("Total MTM equity", f"${schwab_snapshot.total_portfolio_equity:,.2f}")
            if settings.schwab_live_equity_at:
                st.caption(
                    f"Governor using live equity (cached {settings.schwab_live_equity_at[:19]} UTC)"
                )

            if schwab_snapshot.holdings:
                hold_rows = []
                for h in schwab_snapshot.holdings:
                    hold_rows.append(
                        {
                            "position": format_holding_label(h),
                            "type": h.asset_type.value,
                            "qty": h.total_quantity,
                            "avg_cost": h.average_cost,
                            "market_value": h.market_value or None,
                            "by_account": holding_breakdown(h),
                        }
                    )
                st.dataframe(pd.DataFrame(hold_rows), use_container_width=True, hide_index=True)

            if st.button(
                "Use total MTM equity as stock/options starting equity", key="schwab_use_equity"
            ):
                settings.starting_equity_stock_options = schwab_snapshot.total_portfolio_equity
                sync_schwab_live_equity(repo, schwab_snapshot)
                repo.save_app_settings(settings)
                st.session_state.app_settings = settings
                st.success(
                    f"Starting equity set to ${schwab_snapshot.total_portfolio_equity:,.2f}; "
                    "drawdown governor will use live liquidationValue."
                )
                st.rerun()

        st.divider()
        st.subheader("Transaction import")
        with st.form("schwab_fetch_form"):
            schwab_account = st.text_input("Account label or hash", value="schwab-tos")
            col_start, col_end = st.columns(2)
            with col_start:
                fetch_start = st.date_input("Start date", value=None, key="schwab_start")
            with col_end:
                fetch_end = st.date_input("End date", value=None, key="schwab_end")
            save_audit = st.checkbox("Save audit CSV to data/raw/", value=True)
            schwab_submitted = st.form_submit_button("Fetch & Import", type="primary")

        if schwab_submitted:
            if not schwab_py_available():
                st.error("Install schwab-py before fetching.")
            elif not schwab_credentials_configured() or not schwab_status["token"]:
                st.error("Configure Schwab credentials and complete OAuth login first.")
            else:
                with st.spinner(f"Fetching {schwab_account}..."):
                    try:
                        result = import_schwab_fetch(
                            account=schwab_account,
                            start=fetch_start,
                            end=fetch_end,
                            save_csv=save_audit,
                            repo=repo,
                        )
                        st.cache_resource.clear()
                        st.success(
                            f"Imported **{result.imported}** events from Schwab API "
                            f"({result.skipped_duplicates} duplicates skipped, "
                            f"{result.review_queue} review-queue items)."
                        )
                        st.metric("Positions assembled", repo.position_count())
                        st.rerun()
                    except Exception as exc:
                        from trading_architect.ingestion.schwab_auth import SchwabAuthExpired

                        if isinstance(exc, SchwabAuthExpired):
                            st.error(str(exc))
                        else:
                            show_ui_error(exc, context="Schwab transaction fetch")

    st.subheader("Import history")
    log = repo.list_import_log(limit=20)
    if log:
        st.dataframe(pd.DataFrame(log), use_container_width=True, hide_index=True)
    else:
        st.caption("No imports yet.")

elif page == "Size a Trade":
    st.header("Size a Trade")
    st.caption("Six-layer sizing — binding constraint explained (PRD §7.3)")

    from trading_architect.engines.sizing import CandidateTrade, recommend_size
    from trading_architect.models.entities import AssetType, Direction

    col_silo, col_asset = st.columns(2)
    with col_silo:
        silo_val = st.selectbox("Silo", ["stock_options", "futures"], key="size_silo")
    with col_asset:
        asset_val = st.selectbox("Asset", ["stock", "option", "future"], key="size_asset")

    silo = Silo(silo_val)
    silo_state = book.stock_options if silo == Silo.STOCK_OPTIONS else book.futures

    underlying = st.text_input("Underlying", "AAPL", key="size_underlying").upper()
    entry = st.number_input("Entry price", value=100.0, min_value=0.01, key="size_entry")
    spot = st.number_input("Spot (optional)", value=0.0, min_value=0.0, key="size_spot")

    stop = premium = delta = None
    if asset_val == "option":
        premium = st.number_input(
            "Premium per contract", value=5.0, min_value=0.01, key="size_premium"
        )
        delta = st.slider("Delta", 0.05, 0.95, 0.45, key="size_delta")
    else:
        stop = st.number_input("Stop price", value=95.0, min_value=0.01, key="size_stop")

    atr = st.number_input(
        "ATR (optional, vol normalization)", value=0.0, min_value=0.0, key="size_atr"
    )

    st.subheader("Book context")
    st.caption(
        f"Silo equity **${silo_state.equity:,.0f}** · open risk **${silo_state.open_dollar_risk:,.0f}** · "
        f"drawdown **{book.effective_drawdown_pct:.1%}** ({book.effective_governor.state.value})"
    )

    if st.button("Recommend size", type="primary", key="size_btn"):
        candidate = CandidateTrade(
            silo=silo,
            underlying=underlying,
            direction=Direction.LONG,
            asset_type=AssetType(asset_val),
            entry_price=entry,
            stop_price=stop,
            premium_per_contract=premium,
            spot_price=spot or entry,
            option_delta=delta,
            atr=atr if atr > 0 else None,
            symbol=underlying,
        )
        exposure = book.exposure_for_silo(silo)
        rec = recommend_size(candidate, exposure, config=settings.sizing, events=events)
        st.metric("Recommended size", f"{rec.recommended_qty_int:,} units")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Dollar risk", f"${rec.dollar_risk:,.0f}")
        m2.metric("Delta notional", f"${rec.delta_notional:,.0f}")
        m3.metric("Heat after", f"{rec.heat_after:.1%}")
        m4.metric("Leverage after", f"{rec.leverage_after:.2f}×")
        st.info(rec.rationale)
        st.write(f"**Binding constraint:** `{rec.binding_constraint.value}`")
        if rec.warnings:
            for w in rec.warnings:
                st.warning(w)
        with st.expander("Layer breakdown"):
            layer_rows = [
                {
                    "layer": f"L{layer.layer}",
                    "name": layer.name,
                    "qty": round(layer.recommended_qty, 2),
                    "detail": layer.detail,
                }
                for layer in rec.layers
            ]
            st.dataframe(pd.DataFrame(layer_rows), use_container_width=True)

elif page == "Option Selector":
    st.header("Option Selector")
    st.caption("Greeks-based contract ranking — expression optimization only (PRD §7.5)")

    from trading_architect.engines.options_selector import (
        OptionDirection,
        OptionSelectorInput,
        rank_contracts,
    )
    from trading_architect.ingestion.chain import parse_chain_csv

    c1, c2, c3 = st.columns(3)
    with c1:
        underlying = st.text_input("Underlying", "NVDA", key="opt_underlying").upper()
    with c2:
        spot = st.number_input("Spot", value=200.0, min_value=0.01, key="opt_spot")
    with c3:
        target = st.number_input("Target price", value=210.0, min_value=0.01, key="opt_target")

    c4, c5, c6 = st.columns(3)
    with c4:
        direction = st.selectbox("Direction", ["long_call", "long_put"], key="opt_dir")
    with c5:
        hold_days = st.number_input("Expected hold (days)", value=60, min_value=1, key="opt_hold")
    with c6:
        path = st.selectbox("Path", ["gradual", "fast"], key="opt_path")

    iv_rank = st.slider("IV rank (optional, 0–1)", 0.0, 1.0, 0.0, key="opt_iv_rank")
    iv_rank_val = iv_rank if iv_rank > 0 else None

    chain_file = st.file_uploader("Option chain CSV", type=["csv"], key="opt_chain")
    fetch_live = st.button("Fetch live chain (Schwab)", key="opt_fetch_schwab")
    recent_snapshots = repo.list_chain_snapshots(underlying if underlying else None)
    if recent_snapshots and not chain_file:
        st.caption(f"{len(recent_snapshots)} saved chain snapshot(s) for {underlying or 'all'}.")

    st.caption(
        f"Stock/options equity ${book.stock_options.equity:,.0f} · "
        f"heat headroom {max(0, settings.sizing.heat_cap - book.stock_options.heat):.0%}"
    )

    def _run_option_rank(snapshot):
        repo.save_chain_snapshot(snapshot)
        return rank_contracts(
            snapshot,
            OptionSelectorInput(
                underlying=underlying,
                direction=OptionDirection(direction),
                target_price=target,
                expected_hold_days=int(hold_days),
                path=path,
            ),
            book.exposure_for_silo(Silo.STOCK_OPTIONS),
            config=settings.option_selector,
            events=events,
            top_n=6,
        )

    def _display_option_results(result):
        st.warning(result.disclaimer)
        if not result.ranked:
            st.info("No contracts matched. Check DTE band (90–365 + buffer) and chain columns.")
            return
        rows = []
        for item in result.ranked:
            c = item.contract
            sr = item.size_recommendation
            rows.append(
                {
                    "rank": item.rank,
                    "right": c.right,
                    "strike": c.strike,
                    "expiry": str(c.expiry),
                    "dte": c.dte,
                    "premium": round(item.premium, 2),
                    "score": round(item.composite_score, 3),
                    "proj_return_%": round(item.projected_return_pct, 1),
                    "proj_R": round(item.projected_r_multiple, 2),
                    "contracts": sr.recommended_qty_int if sr else 0,
                    "delta": c.delta,
                    "theta": c.theta,
                    "vega": c.vega,
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
        for item in result.ranked:
            with st.expander(f"#{item.rank} {item.contract.right} ${item.contract.strike:.0f}"):
                st.write(item.rationale)
                if item.size_recommendation:
                    st.caption(item.size_recommendation.rationale.replace("**", ""))
                for note in item.tradeoff_notes:
                    st.write(f"• {note}")

    if fetch_live:
        schwab_status = schwab_connection_status()
        if not schwab_status.get("token"):
            st.error("Schwab not connected. Complete OAuth setup under Import Data → Schwab API.")
        else:
            with st.spinner(f"Fetching {underlying} option chain from Schwab..."):
                try:
                    from trading_architect.ingestion.schwab_rest import fetch_chain_snapshot

                    snapshot = fetch_chain_snapshot(underlying)
                    if spot:
                        snapshot = snapshot.model_copy(update={"spot_price": spot})
                    result = _run_option_rank(snapshot)
                    st.success(f"Fetched {len(snapshot.contracts)} contracts from Schwab.")
                    _display_option_results(result)
                except Exception as exc:
                    from trading_architect.ingestion.schwab_auth import SchwabAuthExpired

                    if isinstance(exc, SchwabAuthExpired):
                        st.error(str(exc))
                    else:
                        show_ui_error(exc, context="Schwab option chain fetch")

    if chain_file and st.button("Rank contracts", type="primary", key="opt_btn"):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp.write(chain_file.getvalue())
            tmp_path = Path(tmp.name)

        snapshot = parse_chain_csv(tmp_path, underlying, spot, iv_rank=iv_rank_val)

        result = _run_option_rank(snapshot)
        _display_option_results(result)

elif page == "Current Positions":
    st.header("Current Positions")
    st.caption("Blended exposure vs heat/leverage caps and drawdown governor (PRD §8.1)")

    silo_filter = st.selectbox("Silo", ["All", "stock_options", "futures"])
    status_filter = st.selectbox("Status", ["open", "All", "closed"], index=0)

    filtered = repo.list_positions(
        silo=None if silo_filter == "All" else silo_filter,
        status=None if status_filter == "All" else status_filter,
    )
    filtered = [p for p in filtered if (p.underlying or "").strip()]

    if not filtered:
        st.info("No positions match. Import broker CSVs to get started.")
    else:
        cfg = settings.sizing
        rows = []
        for p in filtered:
            silo_state = book.stock_options if p.silo == Silo.STOCK_OPTIONS else book.futures
            pos_heat = p.total_dollar_risk / silo_state.equity if silo_state.equity > 0 else 0.0
            pos_lev = p.current_delta_notional / silo_state.equity if silo_state.equity > 0 else 0.0
            rows.append(
                {
                    "underlying": p.underlying,
                    "accounts": stock_account_breakdown(p),
                    "silo": p.silo.value,
                    "direction": p.direction.value,
                    "status": p.status.value,
                    "total_risk": p.total_dollar_risk,
                    "pos_heat": pos_heat,
                    "notional": p.current_delta_notional,
                    "pos_leverage": pos_lev,
                    "premium_at_risk": p.premium_at_risk,
                    "stop_risk": p.stop_risk,
                    "realized_pnl": p.realized_pnl,
                    "epoch": p.epoch_id,
                    "governor": book.effective_governor.state.value,
                }
            )
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        if status_filter in ("open", "All"):
            open_by_silo = (
                df[df["status"] == "open"]
                .groupby("silo")
                .agg(
                    risk=("total_risk", "sum"),
                    notional=("notional", "sum"),
                )
            )
            if not open_by_silo.empty:
                st.subheader("Silo totals vs caps")
                cap_rows = []
                for silo_name in open_by_silo.index:
                    state = book.stock_options if silo_name == "stock_options" else book.futures
                    cap_rows.append(
                        {
                            "silo": silo_name,
                            "open_risk": state.open_dollar_risk,
                            "heat": state.heat,
                            "heat_cap": cfg.heat_cap,
                            "heat_status": cap_status_pct(state.heat, cfg.heat_cap),
                            "notional": state.open_delta_notional,
                            "leverage": state.leverage,
                            "leverage_cap": cfg.leverage_cap,
                            "lev_status": cap_status_pct(state.leverage, cfg.leverage_cap),
                            "drawdown": state.drawdown_pct,
                            "governor": state.governor.state.value,
                        }
                    )
                st.dataframe(pd.DataFrame(cap_rows), use_container_width=True, hide_index=True)

elif page == "Alpha Left on Table":
    st.header("Alpha Left on the Table")
    st.caption("Counterfactual sizing replay — entry/exit timing fixed (PRD §7.6)")

    from trading_architect.engines.evaluation import CounterfactualRule, evaluate_alpha_left

    rule_labels = {
        "1% fractional risk": CounterfactualRule.FRACTIONAL_1PCT,
        "2% fractional risk": CounterfactualRule.FRACTIONAL_2PCT,
        "¼ Kelly": CounterfactualRule.KELLY_QUARTER,
        "⅓ Kelly": CounterfactualRule.KELLY_THIRD,
        "½ Kelly": CounterfactualRule.KELLY_HALF,
    }
    selected_rule_label = st.selectbox("Counterfactual rule", list(rule_labels.keys()))
    selected_rule = rule_labels[selected_rule_label]

    st.caption(
        f"Starting equity — stock/options ${settings.starting_equity_stock_options:,.0f}, "
        f"futures ${settings.starting_equity_futures:,.0f} (edit in Settings)"
    )

    if not events:
        st.info("Import broker CSVs first.")
    else:
        report = evaluate_alpha_left(events, starting_equity=settings.starting_equity())

        summary_rows = []
        for seg in report.segments:
            if seg.rule != selected_rule:
                continue
            summary_rows.append(
                {
                    "silo": seg.silo.value,
                    "epoch": seg.epoch_id,
                    "trades": seg.trade_count,
                    "actual_pnl": seg.total_actual_pnl,
                    "counterfactual_pnl": seg.total_counterfactual_pnl,
                    "alpha_left": seg.alpha_left_on_table,
                }
            )

        if summary_rows:
            st.subheader("Summary")
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

            for seg in report.segments:
                if seg.rule != selected_rule:
                    continue
                if seg.r_distribution:
                    rd = seg.r_distribution
                    st.subheader(f"R-distribution — {seg.silo.value} / {seg.epoch_id}")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Trades", rd.count)
                    m2.metric("Expectancy", f"{rd.expectancy:.2f}R")
                    m3.metric("Win rate", f"{rd.win_rate:.0%}")
                    m4.metric("Optimal-f", f"{rd.optimal_f:.3f}")
                    st.write(
                        f"Mean {rd.mean_r:.2f}R · Std {rd.std_r:.2f} · Skew {rd.skew:.2f} · "
                        f"P5/P95 {rd.p05_r:.2f}/{rd.p95_r:.2f}R"
                    )
                if seg.options_winner_analysis:
                    st.info(seg.options_winner_analysis.narrative)

            drill = [
                seg for seg in report.segments if seg.rule == selected_rule and seg.contributions
            ]
            if drill and st.checkbox("Show per-trade drill-down"):
                for seg in drill:
                    st.write(f"**{seg.silo.value} / {seg.epoch_id}**")
                    contrib_rows = [
                        {
                            "underlying": c.underlying,
                            "symbol": c.symbol,
                            "opened": c.opened_at[:10],
                            "actual_pnl": c.actual_pnl,
                            "cf_pnl": c.counterfactual_pnl,
                            "alpha_left": c.alpha_left,
                            "R": c.realized_r,
                            "risk_basis": c.risk_basis,
                        }
                        for c in seg.contributions
                    ]
                    st.dataframe(
                        pd.DataFrame(contrib_rows), use_container_width=True, hide_index=True
                    )
        else:
            st.info("No closed trades found for evaluation.")

elif page == "Edge & Risk Review":
    st.header("Edge & Risk Review")
    st.caption("Bootstrap edge estimates and confidence-gated recommendations (PRD §7.4)")

    from trading_architect.engines.adaptive_risk import RiskAction, build_risk_review

    live = settings.sizing
    st.caption(
        f"Current base f **{live.base_risk_f:.2%}** · Kelly **{live.kelly_fraction:.0%}** · "
        f"drawdown **{book.effective_drawdown_pct:.1%}** "
        f"(edit live f under Settings → Sizing & caps)"
    )

    if not events:
        st.info("Import broker CSVs first.")
    else:
        report = build_risk_review(
            events,
            config=settings.adaptive_risk,
            sizing=settings.sizing,
            drawdown_pct=book.effective_drawdown_pct,
        )

        if report.post_epoch_id:
            st.caption(f"Step-up decisions use post-change epoch: `{report.post_epoch_id}`")

        for rec in report.recommendations:
            st.subheader(rec.silo.value.replace("_", " + ").title())
            action_colors = {
                RiskAction.STEP_UP_KELLY: "success",
                RiskAction.STEP_UP_BASE_F: "success",
                RiskAction.DE_RISK: "error",
                RiskAction.INSUFFICIENT_DATA: "warning",
                RiskAction.HOLD: "info",
            }
            msg_fn = getattr(st, action_colors.get(rec.action, "info"))
            msg_fn(rec.narrative)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Action", rec.action.value.replace("_", " "))
            m2.metric("Effective f", f"{rec.effective_risk_f:.2%}")
            m3.metric(
                "Kelly", f"{rec.current_kelly_fraction:.0%} → {rec.recommended_kelly_fraction:.0%}"
            )
            m4.metric("Base f", f"{rec.current_base_f:.2%} → {rec.recommended_base_f:.2%}")

            if rec.withhold_reason:
                st.warning(rec.withhold_reason)
            for w in rec.warnings:
                st.warning(w)

            with st.expander("Statistics"):
                st.text(rec.statistics)

        if report.epoch_segments:
            st.subheader("Epoch-segmented edge")
            seg_rows = []
            for seg in report.epoch_segments:
                rd = seg.r_distribution
                seg_rows.append(
                    {
                        "silo": seg.silo.value,
                        "epoch": seg.epoch_id,
                        "trades": seg.trade_count,
                        "expectancy": round(rd.expectancy, 2) if rd else None,
                        "win_rate": f"{rd.win_rate:.0%}" if rd else None,
                        "optimal_f": round(seg.optimal_f_ci.point, 3) if seg.optimal_f_ci else None,
                        "optimal_f_lower": round(seg.optimal_f_ci.lower, 3)
                        if seg.optimal_f_ci
                        else None,
                        "step_up_ready": seg.sufficient_for_step_up,
                    }
                )
            st.dataframe(pd.DataFrame(seg_rows), use_container_width=True, hide_index=True)

elif page == "Review Queue":
    st.header("Review Queue")
    items = repo.list_review_queue()
    if not items:
        st.success("Review queue is empty.")
    else:
        for item in items[:50]:
            with st.expander(f"{item.source_file} row {item.row_index}: {item.reason}"):
                st.json(item.raw_row)

elif page == "Settings":
    st.header("Settings")
    st.caption(
        "All parameters user-inspectable and overridable — changes logged (PRD §7.3 FR-3.8, §8.6)"
    )

    from trading_architect.models.entities import MethodologyEpoch

    tab_equity, tab_sizing, tab_risk, tab_epochs, tab_log = st.tabs(
        ["Starting equity", "Sizing & caps", "Risk appetite", "Epochs", "Override log"]
    )

    draft = app_settings_from_json(app_settings_to_json(settings))

    with tab_equity:
        draft.starting_equity_stock_options = st.number_input(
            "Starting equity — stock/options",
            value=float(draft.starting_equity_stock_options),
            step=10_000.0,
            key="set_eq_stock",
        )
        draft.starting_equity_futures = st.number_input(
            "Starting equity — futures",
            value=float(draft.starting_equity_futures),
            step=10_000.0,
            key="set_eq_fut",
        )
        st.caption(
            "Used for equity reconstruction, alpha-left counterfactuals, and sizing denominators."
        )

    with tab_sizing:
        sc = draft.sizing
        c1, c2 = st.columns(2)
        sc.base_risk_f = c1.number_input(
            "Base risk f", value=float(sc.base_risk_f), format="%.4f", step=0.001
        )
        sc.kelly_fraction = c2.number_input(
            "Kelly fraction", value=float(sc.kelly_fraction), format="%.4f", step=0.05
        )
        c3, c4 = st.columns(2)
        sc.heat_cap = c3.number_input(
            "Heat cap", value=float(sc.heat_cap), format="%.4f", step=0.01
        )
        sc.leverage_cap = c4.number_input(
            "Leverage cap (×)", value=float(sc.leverage_cap), step=0.1
        )
        st.subheader("Drawdown governor thresholds")
        d1, d2, d3 = st.columns(3)
        sc.drawdown_soft = d1.number_input(
            "Soft alert", value=float(sc.drawdown_soft), format="%.4f", step=0.01
        )
        sc.drawdown_throttle_start = d2.number_input(
            "Throttle start", value=float(sc.drawdown_throttle_start), format="%.4f", step=0.01
        )
        sc.drawdown_hard = d3.number_input(
            "Hard cap", value=float(sc.drawdown_hard), format="%.4f", step=0.01
        )

    with tab_risk:
        ar = draft.adaptive_risk
        st.caption(
            f"Live base f and Kelly fraction are set under **Sizing & caps** "
            f"({draft.sizing.base_risk_f:.2%} base f, {draft.sizing.kelly_fraction:.0%} Kelly)."
        )
        ar.min_trades_for_step_up = st.number_input(
            "Min trades for step-up",
            value=int(ar.min_trades_for_step_up),
            min_value=5,
            step=1,
        )

    with tab_epochs:
        epochs = repo.list_epochs()
        for epoch in epochs:
            with st.expander(f"{epoch.label} (`{epoch.epoch_id}`)"):
                new_start = st.date_input(
                    "Start date",
                    value=epoch.start_date,
                    key=f"epoch_start_{epoch.epoch_id}",
                )
                end_val = epoch.end_date or date.today()
                new_end_raw = st.date_input(
                    "End date (leave as today for open-ended)",
                    value=end_val,
                    key=f"epoch_end_{epoch.epoch_id}",
                )
                new_label = st.text_input(
                    "Label", value=epoch.label, key=f"epoch_label_{epoch.epoch_id}"
                )
                new_notes = st.text_area(
                    "Notes", value=epoch.notes, key=f"epoch_notes_{epoch.epoch_id}"
                )
                if st.button("Update epoch", key=f"epoch_save_{epoch.epoch_id}"):
                    updated = MethodologyEpoch(
                        epoch_id=epoch.epoch_id,
                        start_date=new_start,
                        end_date=None
                        if new_end_raw >= date.today() and epoch.end_date is None
                        else new_end_raw,
                        label=new_label,
                        notes=new_notes,
                    )
                    repo.update_epoch(updated)
                    st.success(f"Updated {epoch.epoch_id}")
                    st.rerun()

    with tab_log:
        log = repo.list_override_log(limit=50)
        if log:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "when": e.timestamp.strftime("%Y-%m-%d %H:%M"),
                            "parameter": e.parameter,
                            "old": e.old_value,
                            "new": e.new_value,
                            "source": e.source,
                        }
                        for e in log
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No overrides logged yet.")

    st.subheader("Store")
    m1, m2 = st.columns(2)
    m1.metric("Trade events", repo.event_count())
    m2.metric("Positions", repo.position_count())

    st.subheader("Robinhood API")
    if robin_stocks_available():
        st.write("robin-stocks: installed")
    else:
        st.write("robin-stocks: not installed (`pip install robin-stocks`)")
    if os.environ.get("RH_USERNAME"):
        st.write(f"RH_USERNAME set: {os.environ['RH_USERNAME']}")
    else:
        st.write("RH_USERNAME: not set (enter credentials in Import → Robinhood API tab)")

    st.subheader("Schwab API")
    schwab_status = schwab_connection_status()
    st.write(f"schwab-py: {'installed' if schwab_status['installed'] else 'not installed'}")
    st.write(f"Credentials: {'configured' if schwab_status['configured'] else 'not set'}")
    st.write(f"Token: {'present' if schwab_status['token'] else 'missing'}")
    st.caption(str(schwab_status["message"]))
    if draft.schwab_live_equity is not None and draft.schwab_live_equity_at:
        st.caption(
            f"Cached MTM equity for governor: ${draft.schwab_live_equity:,.2f} "
            f"(as of {draft.schwab_live_equity_at[:19]})"
        )

    if schwab_status.get("token"):
        if st.button("Refresh account list from Schwab", key="schwab_refresh_accounts"):
            try:
                from trading_architect.ingestion.schwab_auth import SchwabAuthExpired, get_client

                list_accounts(client=get_client(), repo=repo)
                draft = repo.load_app_settings()
                st.session_state.settings_draft = draft
                st.success("Account label mapping updated.")
                st.rerun()
            except SchwabAuthExpired as exc:
                st.error(str(exc))
            except Exception as exc:
                show_ui_error(exc, context="Schwab account refresh")

    if draft.schwab_account_hashes:
        st.caption("Friendly label → account hash (plain account numbers are never stored).")
        mapping_rows = [
            {"label": label, "hash_prefix": f"{hash_val[:8]}…"}
            for label, hash_val in sorted(draft.schwab_account_hashes.items())
        ]
        st.dataframe(pd.DataFrame(mapping_rows), use_container_width=True, hide_index=True)
        new_label = st.text_input("Add label", value="", key="schwab_new_label")
        new_hash = st.text_input("Hash value", value="", key="schwab_new_hash")
        if st.button("Add mapping", key="schwab_add_mapping") and new_label and new_hash:
            draft.schwab_account_hashes[new_label.strip()] = new_hash.strip()
            st.session_state.settings_draft = draft
            st.rerun()
    else:
        st.caption(
            "No Schwab accounts mapped yet. Connect under Import Data → Schwab, "
            "or click Refresh account list above."
        )

    if st.button("Save settings", type="primary"):
        save_settings(repo, draft)
        st.success("Settings saved.")
        st.rerun()
