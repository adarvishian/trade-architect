"""Option selector branch — folded into Size a Trade (Phase 3)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
from ui_helpers import show_ui_error

from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.book_context import BookContext
from trading_architect.engines.options_selector import (
    OptionDirection,
    OptionSelectorInput,
    rank_contracts,
)
from trading_architect.ingestion.chain import parse_chain_csv
from trading_architect.models.entities import Silo
from trading_architect.store.repository import Repository


def render_option_selector_branch(
    repo: Repository,
    settings: AppSettings,
    events: list,
    book: BookContext,
    underlying: str,
) -> None:
    st.subheader("Option contract selector")
    st.caption("Greeks-based contract ranking — expression optimization only.")

    c1, c2 = st.columns(2)
    with c1:
        spot = st.number_input("Spot", value=200.0, min_value=0.01, key="opt_spot")
    with c2:
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
                    "R:R": round(sr.reward_risk_ratio, 2) if sr and sr.reward_risk_ratio is not None else None,
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
        from trading_architect.ingestion.schwab_auth import schwab_connection_status

        schwab_status = schwab_connection_status()
        if not schwab_status.get("token"):
            st.error(
                "Schwab not connected. Complete OAuth setup under Accounts → Backfill history."
            )
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

    if chain_file and st.button("Rank contracts", type="secondary", key="opt_btn"):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp.write(chain_file.getvalue())
            tmp_path = Path(tmp.name)

        try:
            snapshot = parse_chain_csv(tmp_path, underlying, spot, iv_rank=iv_rank_val)
            result = _run_option_rank(snapshot)
            _display_option_results(result)
        finally:
            tmp_path.unlink(missing_ok=True)
