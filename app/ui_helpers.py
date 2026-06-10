"""Streamlit UI helpers — sidebar governor, exposure badges (M6)."""

from __future__ import annotations

import streamlit as st

from trading_architect.engines.book_context import BookContext, SiloBookState
from trading_architect.engines.drawdown import DrawdownGovernorState


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
