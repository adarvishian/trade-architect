"""Schwab REST market data — option chains and quotes (PRD §16.4, Phase 3)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx

from trading_architect.engines.marks import Mark
from trading_architect.ingestion.schwab_auth import get_client
from trading_architect.ingestion.schwab_symbols import from_occ, to_occ
from trading_architect.models.entities import ChainContract, ChainSnapshot

MARKETDATA_BASE = "https://api.schwabapi.com/marketdata/v1"


def _unwrap_schwab_client(client: Any) -> Any:
    """Return the underlying schwab-py client (past SchwabSession wrapper)."""
    return getattr(client, "_client", client)


def _option_chain_enums(client: Any) -> tuple[Any, Any]:
    """ContractType.ALL and Strategy.SINGLE for schwab-py (enforce_enums=True)."""
    inner = _unwrap_schwab_client(client)
    options = getattr(inner, "Options", None)
    if options is not None:
        return options.ContractType.ALL, options.Strategy.SINGLE
    # Test fakes and other stubs without schwab-py enum types.
    return "ALL", "SINGLE"


def _normalize_iv(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        iv = float(raw)
    except (TypeError, ValueError):
        return None
    if iv <= 0:
        return None
    return iv / 100.0 if iv > 3 else iv


def _float_or_none(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _int_or_none(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_exp_key(exp_key: str, asof: date) -> tuple[date, int]:
    """Parse Schwab exp-date-map key like '2026-09-18:120'."""
    date_part = exp_key.split(":")[0]
    expiry = date.fromisoformat(date_part)
    dte = (expiry - asof).days
    return expiry, max(dte, 0)


def _contract_from_schwab(
    raw: dict,
    *,
    expiry: date,
    dte: int,
    right: str,
) -> ChainContract | None:
    strike = _float_or_none(raw.get("strikePrice"))
    if strike is None:
        return None

    bid = _float_or_none(raw.get("bid"))
    ask = _float_or_none(raw.get("ask"))
    mid = _float_or_none(raw.get("mark"))
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2

    put_call = str(raw.get("putCall", "")).upper()
    if put_call.startswith("C"):
        right = "C"
    elif put_call.startswith("P"):
        right = "P"

    dte_val = _int_or_none(raw.get("daysToExpiration"))
    if dte_val is not None:
        dte = dte_val

    return ChainContract(
        strike=strike,
        expiry=expiry,
        dte=dte,
        bid=bid,
        ask=ask,
        mid=mid,
        iv=_normalize_iv(raw.get("volatility")),
        delta=_float_or_none(raw.get("delta")),
        gamma=_float_or_none(raw.get("gamma")),
        theta=_float_or_none(raw.get("theta")),
        vega=_float_or_none(raw.get("vega")),
        open_interest=_int_or_none(raw.get("openInterest")),
        right=right,
    )


def parse_option_chain_response(
    data: dict,
    underlying: str,
    *,
    min_dte: int = 0,
    max_dte: int = 9999,
    asof: datetime | None = None,
    risk_free_rate: float = 0.05,
) -> ChainSnapshot:
    """Map raw Schwab GET /chains JSON to ChainSnapshot."""
    asof_dt = asof or datetime.now()
    asof_date = asof_dt.date()

    underlying_block = data.get("underlying") or {}
    spot = _float_or_none(underlying_block.get("last"))
    if spot is None:
        spot = _float_or_none(underlying_block.get("mark"))
    if spot is None:
        spot = _float_or_none(data.get("underlyingPrice"))
    if spot is None:
        raise ValueError("Option chain response missing underlying price")

    contracts: list[ChainContract] = []
    for exp_map, default_right in (
        (data.get("callExpDateMap") or {}, "C"),
        (data.get("putExpDateMap") or {}, "P"),
    ):
        for exp_key, strikes in exp_map.items():
            expiry, dte = _parse_exp_key(exp_key, asof_date)
            if dte < min_dte or dte > max_dte:
                continue
            for _strike_key, leg_list in (strikes or {}).items():
                for raw in leg_list or []:
                    contract = _contract_from_schwab(
                        raw,
                        expiry=expiry,
                        dte=dte,
                        right=default_right,
                    )
                    if contract is not None:
                        contracts.append(contract)

    return ChainSnapshot(
        underlying=underlying.upper(),
        asof_timestamp=asof_dt,
        spot_price=spot,
        risk_free_rate=risk_free_rate,
        contracts=contracts,
    )


def _raise_for_response(resp: Any, context: str) -> dict:
    status = getattr(resp, "status_code", None)
    if status is not None and status != httpx.codes.OK:
        body = ""
        try:
            body = resp.text[:200]
        except Exception:
            pass
        if status == 401:
            raise PermissionError(f"Schwab auth failed ({context}). Re-authenticate with --login.")
        if status == 429:
            raise RuntimeError(f"Schwab rate limit hit ({context}). Retry shortly.")
        raise RuntimeError(f"Schwab API error {status} ({context}): {body}")

    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected Schwab response for {context}")
    return data


def fetch_chain_snapshot(
    underlying: str,
    *,
    strike_count: int = 20,
    min_dte: int = 90,
    max_dte: int = 400,
    client=None,
    risk_free_rate: float = 0.05,
    repo=None,
) -> ChainSnapshot:
    """Pull an option chain from Schwab GET /chains and normalize to ChainSnapshot."""
    client = get_client(client=client)
    asof = datetime.now()
    from_date = asof.date() + timedelta(days=min_dte)
    to_date = asof.date() + timedelta(days=max_dte)
    contract_type, strategy = _option_chain_enums(client)

    resp = client.get_option_chain(
        underlying.upper(),
        contract_type=contract_type,
        strike_count=strike_count,
        from_date=from_date,
        to_date=to_date,
        include_underlying_quote=True,
        strategy=strategy,
    )
    data = _raise_for_response(resp, f"option chain for {underlying}")
    snapshot = parse_option_chain_response(
        data,
        underlying,
        min_dte=min_dte,
        max_dte=max_dte,
        asof=asof,
        risk_free_rate=risk_free_rate,
    )
    if repo is not None:
        repo.save_chain_snapshot(snapshot)
    return snapshot


def parse_quotes_response(data: dict, *, asof: datetime | None = None) -> dict[str, dict]:
    """Normalize GET /quotes response to symbol → {price, delta, asof}."""
    asof_dt = asof or datetime.now()
    out: dict[str, dict] = {}
    for sym, block in (data or {}).items():
        quote = block.get("quote") or block
        price = _float_or_none(quote.get("markPrice"))
        if price is None:
            price = _float_or_none(quote.get("lastPrice"))
        if price is None:
            price = _float_or_none(quote.get("closePrice"))
        delta = _float_or_none(quote.get("delta"))
        canonical = from_occ(sym) or sym.upper()
        out[canonical] = {"price": price, "delta": delta, "asof": asof_dt}
    return out


def fetch_quotes(symbols: list[str], *, client=None) -> dict[str, Mark]:
    """Fetch marks for stock tickers and/or synthetic option symbols via GET /quotes."""
    if not symbols:
        return {}

    client = get_client(client=client)
    occ_map = {to_occ(s): s for s in symbols}
    occ_symbols = list(occ_map.keys())

    resp = client.get_quotes(occ_symbols)
    data = _raise_for_response(resp, "quotes")
    parsed = parse_quotes_response(data)

    result: dict[str, Mark] = {}
    for occ, original in occ_map.items():
        canonical = from_occ(occ) or original.upper()
        row = parsed.get(canonical) or parsed.get(occ)
        if row is None:
            continue
        price = row.get("price")
        if price is None:
            continue
        asof = row.get("asof") or datetime.now()
        result[original] = Mark(
            symbol=original,
            price=float(price),
            delta=row.get("delta"),
            asof=asof if isinstance(asof, datetime) else datetime.now(),
        )
    return result
