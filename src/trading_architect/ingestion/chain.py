"""On-demand option chain import — PRD §6.4 (manual CSV)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from trading_architect.models.entities import ChainContract, ChainSnapshot


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    mapping = {
        "strike": "strike",
        "strike price": "strike",
        "strike_price": "strike",
        "expiry": "expiry",
        "expiration": "expiry",
        "expiration date": "expiry",
        "exp": "expiry",
        "dte": "dte",
        "days to expiration": "dte",
        "type": "right",
        "right": "right",
        "call/put": "right",
        "bid": "bid",
        "ask": "ask",
        "mid": "mid",
        "last": "mid",
        "mark": "mid",
        "iv": "iv",
        "implied vol": "iv",
        "implied volatility": "iv",
        "delta": "delta",
        "gamma": "gamma",
        "theta": "theta",
        "vega": "vega",
        "open interest": "open_interest",
        "oi": "open_interest",
    }
    cols = {c: mapping.get(c.strip().lower(), c.strip().lower()) for c in df.columns}
    return df.rename(columns=cols)


def _parse_right(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "C"
    s = str(val).strip().upper()
    if s in ("C", "CALL", "CALLS"):
        return "C"
    if s in ("P", "PUT", "PUTS"):
        return "P"
    return "C"


def _parse_expiry(val, asof: date) -> tuple[date, int]:
    if isinstance(val, date):
        exp = val
    elif isinstance(val, datetime):
        exp = val.date()
    else:
        exp = pd.to_datetime(val, errors="coerce")
        if pd.isna(exp):
            raise ValueError(f"Cannot parse expiry: {val}")
        exp = exp.date() if hasattr(exp, "date") else exp
    dte = (exp - asof).days
    return exp, max(dte, 0)


def parse_chain_csv(
    path: Path | str,
    underlying: str,
    spot_price: float,
    *,
    asof: datetime | None = None,
    risk_free_rate: float = 0.05,
    iv_rank: float | None = None,
    iv_percentile: float | None = None,
) -> ChainSnapshot:
    """Load a chain export CSV into a ChainSnapshot."""
    df = pd.read_csv(path)
    df = _normalize_columns(df)
    asof_dt = asof or datetime.now()
    asof_date = asof_dt.date()

    contracts: list[ChainContract] = []
    for _, row in df.iterrows():
        if "strike" not in df.columns:
            continue
        strike = float(row["strike"])
        if pd.isna(strike):
            continue

        dte_val = row.get("dte")
        if "expiry" in df.columns and not pd.isna(row.get("expiry")):
            expiry, dte = _parse_expiry(row["expiry"], asof_date)
        elif dte_val is not None and not pd.isna(dte_val):
            dte = int(dte_val)
            expiry = asof_date + pd.Timedelta(days=dte)
            if hasattr(expiry, "date"):
                expiry = expiry.date()
        else:
            continue

        right = _parse_right(row.get("right")) if "right" in df.columns else "C"

        def _f(col: str) -> float | None:
            if col not in df.columns:
                return None
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return None
            return float(v)

        bid, ask, mid = _f("bid"), _f("ask"), _f("mid")
        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2

        oi = row.get("open_interest")
        open_interest = int(oi) if oi is not None and not pd.isna(oi) else None

        contracts.append(
            ChainContract(
                strike=strike,
                expiry=expiry,
                dte=dte,
                bid=bid,
                ask=ask,
                mid=mid,
                iv=_f("iv"),
                delta=_f("delta"),
                gamma=_f("gamma"),
                theta=_f("theta"),
                vega=_f("vega"),
                open_interest=open_interest,
                right=right,
            )
        )

    return ChainSnapshot(
        underlying=underlying.upper(),
        asof_timestamp=asof_dt,
        spot_price=spot_price,
        risk_free_rate=risk_free_rate,
        contracts=contracts,
        iv_rank=iv_rank,
        iv_percentile=iv_percentile,
    )
