"""Fetch Robinhood transaction history via API when CSV export is unavailable (e.g. Roth IRA).

Robinhood does not expose CSV export for all account types. This module pulls
fills from Robinhood's authenticated API and normalizes them to TradeEvents.

Setup (one-time):
  pip install robin-stocks
  ta fetch-robinhood --account robinhood-roth

Credentials are read from environment variables (never stored in the DB):
  RH_USERNAME, RH_PASSWORD, RH_MFA_CODE (if 2FA enabled)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from trading_architect.config.env import load_env
from trading_architect.ingestion.parsing import is_valid_equity_ticker, normalize_ticker
from trading_architect.models.entities import (
    AssetType,
    BrokerAccountBalance,
    ConsolidatedHolding,
    HoldingLeg,
    OptionSpec,
    ReviewQueueItem,
    RobinhoodPortfolioSnapshot,
    Side,
    Silo,
    TradeEvent,
)

_rh_session_active = False


def _require_robin_stocks():
    try:
        import robin_stocks.robinhood as rh  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "robin-stocks is required for API fetch. Install with: pip install robin-stocks"
        ) from exc
    import robin_stocks.robinhood as rh

    return rh


def login_from_env() -> None:
    """Authenticate using RH_USERNAME / RH_PASSWORD / optional RH_MFA_CODE."""
    load_env()
    username = os.environ.get("RH_USERNAME")
    password = os.environ.get("RH_PASSWORD")
    if not username or not password:
        raise ValueError(
            "Set RH_USERNAME and RH_PASSWORD environment variables for Robinhood API login."
        )
    mfa = os.environ.get("RH_MFA_CODE")
    login(username, password, mfa_code=mfa)


def login(username: str, password: str, mfa_code: str | None = None) -> None:
    """Authenticate with explicit credentials (session-only; never persisted)."""
    global _rh_session_active
    rh = _require_robin_stocks()
    rh.login(username, password, mfa_code=mfa_code or None)
    _rh_session_active = True


def robin_stocks_available() -> bool:
    try:
        _require_robin_stocks()
        return True
    except ImportError:
        return False


def _load_all_accounts() -> list[dict]:
    """Return all linked brokerage accounts from Robinhood profile."""
    rh = _require_robin_stocks()
    accounts = rh.load_account_profile(dataType="results")
    if isinstance(accounts, dict):
        return [accounts]
    return list(accounts or [])


def _account_type(acct: dict) -> str:
    return str(acct.get("brokerage_account_type") or acct.get("type") or "").lower()


def _account_label_for_acct(acct: dict) -> str | None:
    """Map Robinhood account dict to our canonical account label."""
    acct_type = _account_type(acct)
    number = str(acct.get("account_number") or acct.get("rhs_account_number") or "")
    if not number:
        return None
    if "roth" in acct_type:
        return "robinhood-roth"
    if "ira" in acct_type:
        return "robinhood-ira"
    if acct_type in {"individual", "margin", "cash", "custodial"}:
        return "robinhood-individual"
    return None


def list_robinhood_accounts() -> list[tuple[str, str]]:
    """Return (account_label, account_number) for each linked Robinhood account."""
    accounts = _load_all_accounts()
    result: list[tuple[str, str]] = []
    for acct in accounts:
        label = _account_label_for_acct(acct)
        if not label:
            continue
        number = str(acct.get("account_number") or acct.get("rhs_account_number") or "")
        if number:
            result.append((label, number))
    return result


def _float_or_zero(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_rh_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _order_timestamp(order: dict, leg: dict | None = None) -> datetime | None:
    """Best-effort fill time — option orders often lack last_transaction_at."""
    for source in (order, leg or {}):
        for key in ("last_transaction_at", "updated_at", "created_at", "timestamp"):
            ts = _parse_rh_timestamp(source.get(key))
            if ts is not None:
                return ts
    return None


def _cash_and_equivalents_from_profile(account_profile: dict) -> tuple[float, float, float]:
    """Return (cash, cash_equivalents, total) from a Robinhood account profile."""
    cash_bal = account_profile.get("cash_balances") or {}
    if isinstance(cash_bal, dict) and cash_bal:
        cash = _float_or_zero(cash_bal.get("cash"))
        portfolio_cash = _float_or_zero(cash_bal.get("total_cash") or cash_bal.get("cash"))
    else:
        cash = _float_or_zero(account_profile.get("cash"))
        portfolio_cash = _float_or_zero(account_profile.get("portfolio_cash"))

    uncleared = _float_or_zero(account_profile.get("uncleared_deposits"))
    total = portfolio_cash if portfolio_cash else cash + uncleared
    if not total:
        total = cash
    equivalents = max(0.0, total - cash)
    return cash, equivalents, total


def fetch_account_balance(account: str, account_number: str | None = None) -> BrokerAccountBalance:
    """Pull cash/cash-equivalent and portfolio equity for one Robinhood account."""
    rh = _require_robin_stocks()
    account_number = account_number or _account_number_for_label(account)
    account_profile = rh.load_account_profile(account_number=account_number) or {}
    portfolio_profile = rh.load_portfolio_profile(account_number=account_number) or {}

    cash, equivalents, total_cash = _cash_and_equivalents_from_profile(account_profile)
    equity = _float_or_zero(portfolio_profile.get("equity"))
    extended = _float_or_zero(portfolio_profile.get("extended_hours_equity"))
    if extended:
        equity = max(equity, extended)

    return BrokerAccountBalance(
        account=account,
        account_type=_account_type(account_profile),
        cash=cash,
        cash_equivalents=equivalents,
        cash_and_equivalents=total_cash,
        portfolio_equity=equity,
        buying_power=_float_or_zero(account_profile.get("buying_power")),
    )


def _option_symbol_from_instrument(instrument: dict, underlying: str) -> tuple[str, OptionSpec]:
    expiry = pd.to_datetime(instrument["expiration_date"]).date()
    strike = float(instrument["strike_price"])
    opt_type = str(instrument.get("type", "call")).lower()
    right = "C" if opt_type == "call" else "P"
    if not underlying:
        underlying = str(instrument.get("chain_symbol") or instrument.get("symbol") or "").upper()
    symbol = f"{underlying}_{expiry.isoformat()}_{right}_{strike}"
    return symbol, OptionSpec(right=right, strike=strike, expiry=expiry)


def fetch_stock_holdings(account: str, account_number: str | None = None) -> list[HoldingLeg]:
    rh = _require_robin_stocks()
    account_number = account_number or _account_number_for_label(account)
    positions = rh.get_open_stock_positions(account_number=account_number) or []

    legs: list[HoldingLeg] = []
    for pos in positions:
        if not pos:
            continue
        qty = _float_or_zero(pos.get("quantity"))
        if qty == 0:
            continue
        instrument_url = pos.get("instrument")
        if not instrument_url:
            continue
        symbol = str(rh.get_symbol_by_url(instrument_url)).upper()
        legs.append(
            HoldingLeg(
                account=account,
                symbol=symbol,
                underlying=symbol,
                asset_type=AssetType.STOCK,
                quantity=qty,
                average_cost=_float_or_zero(pos.get("average_buy_price")),
            )
        )
    return legs


def fetch_option_holdings(account: str, account_number: str | None = None) -> list[HoldingLeg]:
    rh = _require_robin_stocks()
    account_number = account_number or _account_number_for_label(account)
    positions = rh.get_open_option_positions(account_number=account_number) or []

    legs: list[HoldingLeg] = []
    for pos in positions:
        if not pos:
            continue
        qty = _float_or_zero(pos.get("quantity"))
        if qty == 0:
            continue
        option_url = pos.get("option")
        if not option_url:
            continue
        instrument = rh.request_get(option_url)
        underlying = str(pos.get("chain_symbol") or instrument.get("chain_symbol") or "").upper()
        symbol, _ = _option_symbol_from_instrument(instrument, underlying)
        legs.append(
            HoldingLeg(
                account=account,
                symbol=symbol,
                underlying=underlying,
                asset_type=AssetType.OPTION,
                quantity=qty,
                average_cost=_float_or_zero(pos.get("average_price")),
            )
        )
    return legs


def consolidate_holdings(legs: list[HoldingLeg]) -> list[ConsolidatedHolding]:
    """Roll up legs across accounts into sum-of-parts rows keyed by symbol."""
    grouped: dict[str, ConsolidatedHolding] = {}
    cost_weight: dict[str, float] = {}

    for leg in legs:
        key = leg.symbol
        if key not in grouped:
            grouped[key] = ConsolidatedHolding(
                symbol=leg.symbol,
                underlying=leg.underlying,
                asset_type=leg.asset_type,
                total_quantity=0.0,
                by_account={},
            )
            cost_weight[key] = 0.0

        row = grouped[key]
        row.by_account[leg.account] = row.by_account.get(leg.account, 0.0) + leg.quantity
        row.total_quantity += leg.quantity
        cost_weight[key] += abs(leg.quantity) * leg.average_cost
        row.market_value += leg.market_value

    for key, row in grouped.items():
        total_abs = sum(abs(q) for q in row.by_account.values())
        row.average_cost = cost_weight[key] / total_abs if total_abs else 0.0

    return sorted(
        grouped.values(),
        key=lambda h: (h.underlying, h.asset_type.value, h.symbol),
    )


def rh_session_active() -> bool:
    """True when Robinhood login succeeded in this process (session-only)."""
    return _rh_session_active


def fetch_portfolio_snapshot_with_legs(
    accounts: list[str] | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    mfa_code: str | None = None,
    repo=None,
    persist: bool = True,
) -> tuple[RobinhoodPortfolioSnapshot, list[HoldingLeg]]:
    """Fetch balances and open holdings; optionally persist snapshots to the store."""
    global _rh_session_active
    if username and password:
        login(username, password, mfa_code=mfa_code)
    else:
        login_from_env()
    _rh_session_active = True

    linked = list_robinhood_accounts()
    if accounts:
        wanted = {a.lower() for a in accounts}
        linked = [(label, num) for label, num in linked if label.lower() in wanted]

    balances: list[BrokerAccountBalance] = []
    all_legs: list[HoldingLeg] = []
    for label, number in linked:
        balances.append(fetch_account_balance(label, account_number=number))
        all_legs.extend(fetch_stock_holdings(label, account_number=number))
        all_legs.extend(fetch_option_holdings(label, account_number=number))

    snapshot = RobinhoodPortfolioSnapshot(
        fetched_at=datetime.now(timezone.utc),
        accounts=balances,
        holdings=consolidate_holdings(all_legs),
    )

    if persist and repo is not None:
        from trading_architect.services.snapshot_persist import persist_legs_snapshot

        persist_legs_snapshot(
            repo,
            kind="robinhood",
            silo=Silo.STOCK_OPTIONS,
            institution="Robinhood",
            fetched_at=snapshot.fetched_at,
            accounts=balances,
            legs=all_legs,
        )

    return snapshot, all_legs


def fetch_portfolio_snapshot(
    accounts: list[str] | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    mfa_code: str | None = None,
    repo=None,
) -> RobinhoodPortfolioSnapshot:
    """Fetch balances and open holdings for Robinhood Roth + individual (and IRA if linked)."""
    snapshot, _legs = fetch_portfolio_snapshot_with_legs(
        accounts,
        username=username,
        password=password,
        mfa_code=mfa_code,
        repo=repo,
        persist=repo is not None,
    )
    return snapshot


def format_account_breakdown(by_account: dict[str, float]) -> str:
    """Human-readable sum-of-parts, e.g. 'roth: 50 | individual: 100'."""
    if not by_account:
        return ""
    short = {
        "robinhood-roth": "roth",
        "robinhood-individual": "individual",
        "robinhood-ira": "ira",
    }
    parts = []
    for account in sorted(by_account):
        qty = by_account[account]
        if qty == 0:
            continue
        name = short.get(account, account.replace("robinhood-", ""))
        text = str(int(qty)) if qty == int(qty) else f"{qty:g}"
        parts.append(f"{name}: {text}")
    return " | ".join(parts)


def stock_account_breakdown(position) -> str:
    """Sum-of-parts stock shares across accounts for a blended Position."""
    if not position.account_leg_qty:
        return ""
    stock_by_account: dict[str, float] = {}
    for sym, acct_qty in position.account_leg_qty.items():
        if sym != position.underlying:
            continue
        for account, account_qty in acct_qty.items():
            stock_by_account[account] = stock_by_account.get(account, 0.0) + account_qty
    return format_account_breakdown(stock_by_account)


def holding_breakdown(holding: ConsolidatedHolding) -> str:
    return format_account_breakdown(holding.by_account)


def format_holding_label(holding: ConsolidatedHolding) -> str:
    """Display name: ticker for stock; expiry/strike/call|put for options."""
    if holding.asset_type == AssetType.STOCK:
        return holding.underlying
    from trading_architect.ingestion.schwab_symbols import parse_synthetic_option

    parsed = parse_synthetic_option(holding.symbol)
    if parsed:
        ticker, _, spec = parsed
        right = "Call" if spec.right == "C" else "Put"
        return f"{ticker} {spec.expiry} {right} ${spec.strike:g}"
    return holding.symbol.strip()


def format_holding_line(holding: ConsolidatedHolding) -> str:
    """Single-line summary for CLI: label, qty, avg cost, market value, by-account."""
    kind = "shares" if holding.asset_type == AssetType.STOCK else "contracts"
    label = format_holding_label(holding)
    line = f"  {label}: {holding.total_quantity:g} {kind}"
    if holding.average_cost:
        unit = "/sh" if holding.asset_type == AssetType.STOCK else "/contract"
        line += f" | avg ${holding.average_cost:,.2f}{unit}"
    if holding.market_value:
        line += f" | mkt ${holding.market_value:,.2f}"
    breakdown = holding_breakdown(holding)
    if breakdown:
        line += f" ({breakdown})"
    return line


def _account_number_for_label(label: str) -> str:
    """Map friendly account label to Robinhood account_number for API calls."""
    label_lower = label.lower()
    accounts = _load_all_accounts()

    for acct in accounts:
        acct_type = _account_type(acct)
        number = str(acct.get("account_number") or acct.get("rhs_account_number") or "")
        if not number:
            continue
        if "roth" in label_lower and "roth" in acct_type:
            return number
        if "ira" in label_lower and "roth" not in label_lower and "ira" in acct_type:
            return number
        if "individual" in label_lower and acct_type in {
            "individual",
            "margin",
            "cash",
            "custodial",
        }:
            return number

    available = [
        f"{_account_type(a) or 'unknown'} ({a.get('account_number') or a.get('rhs_account_number')})"
        for a in accounts
    ]
    raise ValueError(
        f"No Robinhood account matched label '{label}'. "
        f"Available accounts: {', '.join(available) or 'none found'}"
    )


def _resolve_stock_symbol(order: dict, rh) -> str:
    """Robinhood stock orders reference instruments by URL, not ticker."""
    if order.get("symbol"):
        ticker = normalize_ticker(order["symbol"])
        if is_valid_equity_ticker(ticker):
            return ticker
    instrument_url = order.get("instrument")
    if instrument_url:
        try:
            resolved = rh.get_symbol_by_url(instrument_url)
        except Exception:
            resolved = None
        ticker = normalize_ticker(resolved)
        if is_valid_equity_ticker(ticker):
            return ticker
    return ""


def _resolve_option_leg(order: dict, leg: dict, rh) -> tuple[str, str, OptionSpec]:
    """Resolve option underlying, canonical symbol, and OptionSpec from API leg."""
    underlying = str(order.get("chain_symbol") or "").upper()
    option_url = leg.get("option")
    if not option_url:
        raise ValueError("Option leg missing instrument URL")

    instrument = rh.request_get(option_url)
    expiry = pd.to_datetime(instrument["expiration_date"]).date()
    strike = float(instrument["strike_price"])
    opt_type = str(instrument.get("type", "call")).lower()
    right = "C" if opt_type == "call" else "P"

    if not underlying:
        underlying = str(instrument.get("chain_symbol") or instrument.get("symbol") or "").upper()

    symbol = f"{underlying}_{expiry.isoformat()}_{right}_{strike}"
    spec = OptionSpec(right=right, strike=strike, expiry=expiry)
    return underlying, symbol, spec


def _option_description(underlying: str, spec: OptionSpec) -> str:
    right = "Call" if spec.right == "C" else "Put"
    return f"{underlying} {spec.expiry.month}/{spec.expiry.day}/{spec.expiry.year} {right} ${spec.strike:.2f}"


def fetch_stock_orders(
    account: str, account_number: str | None = None
) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
    rh = _require_robin_stocks()
    account_number = account_number or _account_number_for_label(account)
    orders = rh.get_all_stock_orders(account_number=account_number) or []

    events: list[TradeEvent] = []
    review: list[ReviewQueueItem] = []
    for idx, order in enumerate(orders):
        if order.get("state") != "filled":
            continue

        ts = _order_timestamp(order)
        if ts is None:
            continue

        side = Side.BUY if order.get("side") == "buy" else Side.SELL
        qty = float(order.get("quantity", 0) or order.get("cumulative_quantity", 0))
        price = float(order.get("average_price") or order.get("price", 0))
        symbol = _resolve_stock_symbol(order, rh)
        if not symbol:
            review.append(
                ReviewQueueItem(
                    source_file="robinhood-api",
                    row_index=idx,
                    reason="stock symbol unresolved",
                    raw_row=order,
                )
            )
            continue
        fees = float(order.get("fees") or 0)

        events.append(
            TradeEvent(
                broker="robinhood",
                account=account,
                timestamp=ts,
                symbol=symbol,
                underlying=symbol,
                asset_type=AssetType.STOCK,
                side=side,
                quantity=qty,
                price=price,
                fees=fees,
                silo=Silo.STOCK_OPTIONS,
                raw_ref=f"api::stock_order::{order.get('id')}",
            )
        )
    return events, review


def fetch_option_orders(
    account: str, account_number: str | None = None
) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
    rh = _require_robin_stocks()
    account_number = account_number or _account_number_for_label(account)
    orders = rh.get_all_option_orders(account_number=account_number) or []

    events: list[TradeEvent] = []
    review: list[ReviewQueueItem] = []
    for idx, order in enumerate(orders):
        if order.get("state") != "filled":
            continue

        legs = order.get("legs") or []
        if not legs:
            continue
        leg = legs[0]
        ts = _order_timestamp(order, leg)
        if ts is None:
            continue

        side = Side.BUY if leg.get("side") == "buy" else Side.SELL
        qty = float(leg.get("quantity", 0) or order.get("quantity", 0))
        price = float(order.get("average_price") or order.get("price", 0))
        fees = float(order.get("fees") or 0)

        try:
            underlying, symbol, option_spec = _resolve_option_leg(order, leg, rh)
        except Exception as exc:
            review.append(
                ReviewQueueItem(
                    source_file="robinhood-api",
                    row_index=idx,
                    reason=f"option leg unresolved: {exc}",
                    raw_row=order,
                )
            )
            continue

        events.append(
            TradeEvent(
                broker="robinhood",
                account=account,
                timestamp=ts,
                symbol=symbol,
                underlying=underlying,
                asset_type=AssetType.OPTION,
                side=side,
                quantity=qty,
                price=price,
                fees=fees,
                option_spec=option_spec,
                silo=Silo.STOCK_OPTIONS,
                raw_ref=f"api::option_order::{order.get('id')}",
            )
        )
    return events, review


def fetch_all(
    account: str = "robinhood-roth",
    username: str | None = None,
    password: str | None = None,
    mfa_code: str | None = None,
) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
    """Pull stock + option fills for the named account label."""
    if username and password:
        login(username, password, mfa_code=mfa_code)
    else:
        login_from_env()
    account_number = _account_number_for_label(account)
    stock_events, stock_review = fetch_stock_orders(account, account_number=account_number)
    opt_events, opt_review = fetch_option_orders(account, account_number=account_number)
    events = stock_events + opt_events
    events.sort(key=lambda e: e.timestamp)
    return events, stock_review + opt_review


def _format_rh_date(dt: datetime) -> str:
    return f"{dt.month}/{dt.day}/{dt.year}"


def _event_to_csv_row(e: TradeEvent) -> dict:
    if e.asset_type == AssetType.OPTION and e.option_spec:
        trans = "BTO" if e.side == Side.BUY else "STC"
        desc = _option_description(e.underlying, e.option_spec)
    else:
        trans = "Buy" if e.side == Side.BUY else "Sell"
        desc = e.underlying

    return {
        "Activity Date": _format_rh_date(e.timestamp),
        "Process Date": _format_rh_date(e.timestamp),
        "Settle Date": _format_rh_date(e.timestamp),
        "Instrument": e.underlying,
        "Description": desc,
        "Trans Code": trans,
        "Quantity": e.quantity,
        "Price": f"${e.price:.2f}",
        "Amount": "",
    }


def fetch_and_save_csv(
    account: str,
    dest: Path,
    username: str | None = None,
    password: str | None = None,
    mfa_code: str | None = None,
) -> Path:
    """Fetch via API and write a Robinhood-compatible CSV for audit/re-import."""
    events, _ = fetch_all(
        account=account,
        username=username,
        password=password,
        mfa_code=mfa_code,
    )
    import pandas as pd

    rows = [_event_to_csv_row(e) for e in events]

    dest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(dest, index=False)
    return dest


def save_events_as_csv(events: list[TradeEvent], dest: Path) -> Path:
    """Write already-fetched events to Robinhood-compatible CSV."""
    import pandas as pd

    rows = [_event_to_csv_row(e) for e in events]

    dest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(dest, index=False)
    return dest
