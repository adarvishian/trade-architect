"""Trading Architect — Streamlit UI (M6 polish)."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
from dashboard_ui import render_dashboard
from option_selector_ui import render_option_selector_branch
from ui_helpers import (
    render_app_header,
    render_governor_sidebar,
    render_ops_banner,
    show_ui_error,
)

import trading_architect  # noqa: F401 — loads .env on import
from trading_architect.bootstrap import (
    build_app_book_context,
    create_repository,
    fetch_schwab_portfolio_snapshot,
    import_and_assemble,
    import_robinhood_fetch,
    import_schwab_fetch,
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
    rh_session_active,
    robin_stocks_available,
)
from trading_architect.ingestion.schwab_accounts import list_accounts
from trading_architect.ingestion.schwab_auth import schwab_connection_status, schwab_py_available
from trading_architect.models.entities import Silo
from trading_architect.services.current_state import account_cards
from trading_architect.services.snapshot_persist import persist_manual_balance
from trading_architect.services.sync import sync_all

load_env()

PAGES = [
    "Dashboard",
    "Size a Trade",
    "Accounts",
    "Settings",
]


@st.cache_resource
def get_repository():
    return create_repository()


def maybe_run_sync(repo) -> None:
    """Auto-sync broker snapshots on open and when TTL expires."""
    if "app_settings" not in st.session_state:
        st.session_state.app_settings = repo.load_app_settings()
    settings: AppSettings = st.session_state.app_settings
    ttl = timedelta(minutes=settings.refresh_interval_min)
    now = datetime.now(timezone.utc)
    last = st.session_state.get("last_sync_at")
    force = st.session_state.pop("force_sync", False)
    if force or last is None or (now - last) > ttl:
        results = sync_all(
            repo,
            settings=settings,
            rh_session_active=rh_session_active,
            force=force,
        )
        st.session_state.sync_results = results
        st.session_state.last_sync_at = now


def load_session_data():
    repo = get_repository()
    maybe_run_sync(repo)
    if "app_settings" not in st.session_state:
        st.session_state.app_settings = repo.load_app_settings()
    settings: AppSettings = st.session_state.app_settings
    events = repo.list_events()
    positions = repo.list_positions()
    book = build_app_book_context(events, positions, settings, repo=repo)
    from trading_architect.engines.marks import default_marks_provider
    from trading_architect.services.book_metrics import maybe_record_daily_metrics

    maybe_record_daily_metrics(repo, book, settings=settings, marks_provider=default_marks_provider())
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
default_page = st.session_state.pop("nav_page", PAGES[0])
page_index = PAGES.index(default_page) if default_page in PAGES else 0
page = st.sidebar.radio("Navigate", PAGES, index=page_index)
render_governor_sidebar(book)

render_app_header()
render_ops_banner(st.session_state.get("sync_results"))

if page == "Dashboard":
    render_dashboard(repo, settings, events, book)

elif page == "Accounts":
    st.header("Accounts")
    st.caption("Linked accounts auto-sync on open; manual accounts for banks and Tradovate cash.")

    sync_results = {r.account_label: r for r in st.session_state.get("sync_results", [])}
    cards = account_cards(repo)

    if st.button("Sync now", type="primary", key="accounts_sync_now"):
        st.session_state.force_sync = True
        get_repository.clear()
        st.rerun()

    if cards:
        for card in cards:
            sync = sync_results.get(card["label"])
            status = sync.status if sync else "ok"
            as_of = card["as_of"]
            as_of_text = as_of.strftime("%Y-%m-%d %H:%M UTC") if as_of else "—"
            icon = {"ok": "🟢", "stale": "🟡", "auth_required": "🔴", "error": "🔴"}.get(status, "⚪")
            cols = st.columns([2, 1, 1, 1, 1])
            cols[0].markdown(f"**{card['label']}** ({card['institution'] or card['kind']})")
            cols[1].metric("Value", f"${card['equity_value']:,.0f}" if card["equity_value"] else "—")
            cols[2].metric("Cash", f"${card['cash']:,.0f}" if card["cash"] is not None else "—")
            cols[3].caption(f"As of {as_of_text}")
            cols[4].caption(f"{icon} {status}")
    else:
        st.info("No accounts yet. Add a manual account below or connect Schwab/Robinhood.")

    st.divider()
    review_count = repo.review_queue_count()
    if review_count:
        st.subheader(f"Review queue ({review_count})")
        items = repo.list_review_queue()
        for item in items[:20]:
            with st.expander(f"{item.source_file} row {item.row_index}: {item.reason}"):
                st.json(item.raw_row)
                c1, c2 = st.columns(2)
                if c1.button("Dismiss", key=f"rq_dismiss_{item.item_id}"):
                    repo.dismiss_review_item(item.item_id)
                    st.rerun()
                if c2.button("Resolve", key=f"rq_resolve_{item.item_id}"):
                    repo.resolve_review_item(item.item_id)
                    st.rerun()

    st.divider()
    st.subheader("Manual accounts")
    with st.form("manual_account_form"):
        m_label = st.text_input("Label", placeholder="bank-checking")
        m_institution = st.text_input("Institution", placeholder="Chase")
        m_silo = st.selectbox("Silo", ["stock_options", "futures"])
        m_deployable = st.checkbox("Include in deployable capital", value=True)
        m_submitted = st.form_submit_button("Add account")
    if m_submitted and m_label:
        repo.upsert_account(
            kind="manual",
            label=m_label.strip(),
            silo=Silo(m_silo),
            institution=m_institution.strip(),
            include_in_deployable=m_deployable,
        )
        st.success(f"Added account **{m_label}**")
        st.rerun()

    manual_accounts = [a for a in repo.list_accounts() if a.kind == "manual"]
    if manual_accounts:
        pick = st.selectbox(
            "Update balance",
            [a.label for a in manual_accounts],
            key="manual_balance_account",
        )
        with st.form("manual_balance_form"):
            m_amount = st.number_input("Equity / balance ($)", min_value=0.0, step=100.0)
            m_as_of = st.date_input("As-of date", value=date.today())
            m_bal_submit = st.form_submit_button("Save balance")
        if m_bal_submit and pick:
            acct = repo.get_account_by_label(pick)
            if acct:
                persist_manual_balance(
                    repo,
                    label=pick,
                    silo=acct.silo,
                    institution=acct.institution,
                    equity_value=m_amount,
                    as_of=datetime.combine(m_as_of, datetime.min.time(), tzinfo=timezone.utc),
                )
                st.session_state.force_sync = False
                st.success(f"Balance updated for **{pick}**")
                st.rerun()

    with st.expander("Backfill history (for evaluation)"):
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

                try:
                    result = import_and_assemble(
                        tmp_path, broker=broker, account=account or None, repo=repo
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)

                get_repository.clear()
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
                            get_repository.clear()
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
                                repo=repo,
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
                            get_repository.clear()
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
        render_option_selector_branch(repo, settings, events, book, underlying)
    else:
        stop = st.number_input("Stop price", value=95.0, min_value=0.01, key="size_stop")

    atr = st.number_input(
        "ATR (optional, unused — vol layer removed)", value=0.0, min_value=0.0, key="size_atr"
    )

    silo_cash, silo_bp = None, None
    from trading_architect.engines.capital import silo_brokerage_liquidity

    silo_cash, silo_bp = silo_brokerage_liquidity(repo, silo)
    st.subheader("Book context")
    st.caption(
        f"Silo equity **${silo_state.equity:,.0f}** · open risk **${silo_state.open_dollar_risk:,.0f}** · "
        f"brokerage cash **${silo_cash:,.0f}** · drawdown **{book.effective_drawdown_pct:.1%}** "
        f"({book.effective_governor.state.value})"
    )

    if st.button("Recommend size", type="primary", key="size_btn"):
        import json

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
        exposure = book.exposure_for_silo(silo, repo=repo, settings=settings)
        rec = recommend_size(candidate, exposure, config=settings.sizing, events=events)
        repo.record_size_recommendation(
            ts=datetime.now(timezone.utc),
            silo=silo,
            underlying=underlying,
            asset_type=asset_val,
            recommended_qty=rec.recommended_qty,
            dollar_risk=rec.dollar_risk,
            binding_constraint=rec.binding_constraint.value,
            inputs_json=json.dumps(
                {
                    "entry": entry,
                    "stop": stop,
                    "premium": premium,
                    "spot": spot or entry,
                    "delta": delta,
                }
            ),
            silo_equity=silo_state.equity,
        )
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

elif page == "Settings":
    st.header("Settings")
    st.caption(
        "All parameters user-inspectable and overridable — changes logged (PRD §7.3 FR-3.8, §8.6)"
    )

    from trading_architect.models.entities import MethodologyEpoch

    tab_equity, tab_cashflow, tab_sizing, tab_risk, tab_epochs, tab_log = st.tabs(
        ["Starting equity", "Cashflow", "Sizing & caps", "Risk appetite", "Epochs", "Override log"]
    )

    if "settings_draft" not in st.session_state:
        st.session_state.settings_draft = app_settings_from_json(app_settings_to_json(settings))
    draft: AppSettings = st.session_state.settings_draft

    has_so_snapshots = any(repo.list_accounts(silo=Silo.STOCK_OPTIONS))
    has_fut_snapshots = any(repo.list_accounts(silo=Silo.FUTURES))
    so_fallback = " (fallback)" if has_so_snapshots else ""
    fut_fallback = " (fallback)" if has_fut_snapshots else ""

    with tab_equity:
        draft.starting_equity_stock_options = st.number_input(
            f"Starting equity — stock/options{so_fallback}",
            value=float(draft.starting_equity_stock_options),
            step=10_000.0,
            key="set_eq_stock",
        )
        draft.starting_equity_futures = st.number_input(
            f"Starting equity — futures{fut_fallback}",
            value=float(draft.starting_equity_futures),
            step=10_000.0,
            key="set_eq_fut",
        )
        draft.refresh_interval_min = int(
            st.number_input(
                "Auto-sync interval (minutes)",
                value=int(draft.refresh_interval_min),
                min_value=5,
                max_value=120,
                step=5,
                key="set_refresh_interval",
            )
        )
        st.caption(
            "Fallback equity when no broker snapshots exist; also used for alpha-left evaluation."
        )

    with tab_cashflow:
        draft.monthly_income_after_tax = st.number_input(
            "Monthly income (after tax)",
            value=float(draft.monthly_income_after_tax),
            step=500.0,
            key="set_monthly_income",
        )
        draft.monthly_expenses = st.number_input(
            "Monthly expenses",
            value=float(draft.monthly_expenses),
            step=500.0,
            key="set_monthly_expenses",
        )
        draft.cash_reserve_months = int(
            st.number_input(
                "Cash reserve (months of expenses)",
                value=int(draft.cash_reserve_months),
                min_value=0,
                max_value=24,
                step=1,
                key="set_reserve_months",
            )
        )
        net = draft.monthly_income_after_tax - draft.monthly_expenses
        reserve_amt = draft.cash_reserve_months * draft.monthly_expenses
        st.caption(
            f"Monthly net cashflow: **${net:,.0f}** · Reserve held back: **${reserve_amt:,.0f}**"
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
        st.write("RH_USERNAME: not set (enter credentials in Accounts → Backfill history)")

    st.subheader("Schwab API")
    schwab_status = schwab_connection_status()
    st.write(f"schwab-py: {'installed' if schwab_status['installed'] else 'not installed'}")
    st.write(f"Credentials: {'configured' if schwab_status['configured'] else 'not set'}")
    st.write(f"Token: {'present' if schwab_status['token'] else 'missing'}")
    st.caption(str(schwab_status["message"]))
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
            "No Schwab accounts mapped yet. Connect under Accounts → Backfill history, "
            "or click Refresh account list above."
        )

    if st.button("Save settings", type="primary"):
        save_settings(repo, draft)
        st.session_state.settings_draft = draft
        st.success("Settings saved.")
        st.rerun()
