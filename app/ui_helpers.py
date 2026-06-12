"""Streamlit UI helpers — sidebar governor, exposure badges (M6)."""

from __future__ import annotations

import json
import logging
import subprocess
import traceback
from pathlib import Path

import streamlit as st

from trading_architect import __version__
from trading_architect.config.defaults import DATA_DIR
from trading_architect.engines.book_context import BookContext, SiloBookState
from trading_architect.engines.drawdown import DrawdownGovernorState
from trading_architect.ingestion.schwab_auth import schwab_connection_status
from trading_architect.services.sync import SyncResult

UI_LOG_PATH = DATA_DIR / "trading_architect_ui.log"
logger = logging.getLogger("trading_architect.ui")
_REPO_ROOT = Path(__file__).resolve().parent.parent


@st.cache_data(ttl=60)
def get_git_branch() -> str:
    """Current git branch, or 'unknown' when not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=_REPO_ROOT,
            check=False,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch:
                return branch
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def render_ops_banner(sync_results: list[SyncResult] | None = None) -> None:
    """Global warning when Schwab token is near expiry or any account sync failed."""
    sync_results = sync_results or []
    schwab = schwab_connection_status()
    days_left = schwab.get("days_left")
    if days_left is not None and isinstance(days_left, int) and days_left < 2:
        st.warning(
            f"Schwab refresh token expires in **{days_left}** day(s). "
            "Re-authenticate with `ta fetch-schwab --login` or Settings before sync goes stale."
        )
    for result in sync_results:
        if result.status == "auth_required":
            st.warning(f"**{result.account_label}** — auth required: {result.message}")
        elif result.status == "error":
            st.warning(f"**{result.account_label}** — sync error: {result.message}")


def render_app_header() -> None:
    """Page title with branch and package version in the top-right."""
    col_title, col_meta = st.columns([5, 1])
    with col_title:
        st.title("Trading Architect")
        st.caption(
            "Risk-adjusted position sizing & trade evaluation — import CSVs, size trades, review edge."
        )
    with col_meta:
        branch = get_git_branch()
        st.markdown(
            f'<p style="text-align: right; margin: 0; font-size: 0.8rem; color: #888;">'
            f"<code>{branch}</code><br>v{__version__}"
            f"</p>",
            unsafe_allow_html=True,
        )


def format_ui_error(exc: Exception, *, context: str) -> str:
    """Return a user-safe message; log full traceback for unexpected errors."""
    from trading_architect.ingestion.schwab_auth import SchwabAuthExpired

    if isinstance(exc, SchwabAuthExpired):
        return str(exc)
    if isinstance(exc, ImportError):
        return str(exc)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return f"Network error during {context}. Check your connection and try again."
    if isinstance(exc, (ValueError, KeyError, json.JSONDecodeError, TypeError)):
        return f"Could not parse data during {context}. Check the input format and try again."

    UI_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.error("UI error during %s", context, exc_info=exc)
    with UI_LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n--- {context} ---\n")
        log_file.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    return (
        f"An unexpected error occurred during {context}. Details were logged to {UI_LOG_PATH.name}."
    )


def show_ui_error(exc: Exception, *, context: str) -> None:
    """Display a typed error in Streamlit."""
    st.error(format_ui_error(exc, context=context))


def _governor_badge(state: DrawdownGovernorState) -> str:
    return {
        DrawdownGovernorState.NORMAL: "🟢 Normal",
        DrawdownGovernorState.SOFT_ALERT: "🟡 Soft alert",
        DrawdownGovernorState.THROTTLE: "🟠 Throttle",
        DrawdownGovernorState.HARD: "🔴 Hard cap",
    }[state]


def render_governor_sidebar(book: BookContext) -> None:
    """Always-visible drawdown governor (FR-7.4)."""
    st.sidebar.markdown("---")
    st.sidebar.subheader("Drawdown governor")
    st.sidebar.caption(_governor_badge(book.effective_governor.state))
    hard_cap = 0.20
    st.sidebar.progress(
        min(book.effective_drawdown_pct / hard_cap, 1.0),
        text=f"Effective DD {book.effective_drawdown_pct:.1%} / {hard_cap:.0%}",
    )
    if book.effective_governor.message:
        st.sidebar.warning(book.effective_governor.message)
    else:
        st.sidebar.caption("Within tolerance — full sizing available.")

    for label, state in (
        ("Stock/options", book.stock_options),
        ("Futures", book.futures),
    ):
        st.sidebar.caption(
            f"{label}: ${state.equity:,.0f} · DD {state.drawdown_pct:.1%} · "
            f"heat {state.heat:.0%}/{state.heat_cap:.0%} · lev {state.leverage:.1f}×/{state.leverage_cap:.1f}×"
        )


def cap_status_pct(current: float, cap: float) -> str:
    if cap <= 0:
        return "—"
    ratio = current / cap
    if ratio >= 1.0:
        return "🔴 At cap"
    if ratio >= 0.85:
        return "🟠 Near cap"
    return "🟢 OK"


def silo_book_row(state: SiloBookState) -> dict:
    return {
        "silo": state.silo.value,
        "equity": state.equity,
        "drawdown": state.drawdown_pct,
        "governor": state.governor.state.value,
        "open_risk": state.open_dollar_risk,
        "heat": state.heat,
        "heat_cap": state.heat_cap,
        "heat_status": cap_status_pct(state.heat, state.heat_cap),
        "notional": state.open_delta_notional,
        "leverage": state.leverage,
        "leverage_cap": state.leverage_cap,
        "lev_status": cap_status_pct(state.leverage, state.leverage_cap),
        "open_positions": state.open_position_count,
    }
