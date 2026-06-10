"""Fetch Schwab transaction history via Trader API → TradeEvent[] + review queue."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import httpx

from trading_architect.ingestion.parsing import parse_option_from_text
from trading_architect.ingestion.schwab_accounts import _account_hash_for_label
from trading_architect.ingestion.schwab_auth import SchwabSession, get_client
from trading_architect.ingestion.schwab_symbols import from_occ
from trading_architect.models.entities import (
    AssetType,
    OptionSpec,
    ReviewQueueItem,
    Side,
    Silo,
    TradeEvent,
)

_TRADE_TXN_TYPES = frozenset({"TRADE", "RECEIVE_AND_DELIVER"})
_EXCLUDED_ASSET_TYPES = frozenset({"FUTURE", "FUTURES", "FOREX"})

_SYNTHETIC_OPTION = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})_(?P<exp>\d{4}-\d{2}-\d{2})_(?P<right>[CP])_(?P<strike>[\d.]+)$"
)

_BUY_INSTRUCTIONS = frozenset(
    {
        "BUY",
        "BUY_TO_OPEN",
        "BUY_TO_CLOSE",
        "BUY TO OPEN",
        "BUY TO CLOSE",
        "BUY TO COVER",
    }
)
_SELL_INSTRUCTIONS = frozenset(
    {
        "SELL",
        "SELL_TO_OPEN",
        "SELL_TO_CLOSE",
        "SELL TO OPEN",
        "SELL TO CLOSE",
        "SELL SHORT",
        "SELL_SHORT",
        "EXPIRED",
        "ASSIGNMENT",
    }
)


def _float_or_zero(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_timestamp(raw: Any) -> datetime:
    text = str(raw).replace("Z", "+00:00")
    if "+" not in text[10:] and "T" in text:
        text = text + "+00:00"
    return datetime.fromisoformat(text)


def _normalize_instruction(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", " ").replace("_", " ")


def _side_from_instruction(instruction: str, amount: float) -> Side:
    normalized = _normalize_instruction(instruction)
    compact = normalized.replace(" ", "_")
    if compact in _BUY_INSTRUCTIONS or normalized in _BUY_INSTRUCTIONS:
        return Side.BUY
    if compact in _SELL_INSTRUCTIONS or normalized in _SELL_INSTRUCTIONS:
        return Side.SELL
    return Side.BUY if amount > 0 else Side.SELL


def _fees_from_transfer_items(items: list[dict]) -> float:
    total = 0.0
    for item in items or []:
        fee_type = str(item.get("feeType", "")).upper()
        item_type = str(item.get("type", "")).upper()
        if fee_type or item_type in {"FEE", "COMMISSION"}:
            total += abs(_float_or_zero(item.get("cost") or item.get("amount")))
            continue

        commission = item.get("commissionAndFee") or item.get("commission") or {}
        if isinstance(commission, dict):
            for key in ("commission", "optRegFee", "secFee", "fees"):
                total += abs(_float_or_zero(commission.get(key)))

        fees_block = item.get("fees") or {}
        if isinstance(fees_block, dict):
            for key in ("commission", "optRegFee", "secFee", "totalFees"):
                total += abs(_float_or_zero(fees_block.get(key)))

    return total


def _trade_items(items: list[dict]) -> list[dict]:
    return [item for item in (items or []) if item.get("instrument") and not item.get("feeType")]


def _option_spec_from_synthetic(synthetic: str) -> tuple[str, str, OptionSpec]:
    match = _SYNTHETIC_OPTION.match(synthetic.strip().upper())
    if not match:
        raise ValueError(f"Unparseable option symbol: {synthetic}")
    ticker = match.group("ticker")
    expiry = date.fromisoformat(match.group("exp"))
    right = match.group("right")
    strike = float(match.group("strike"))
    symbol = f"{ticker}_{expiry.isoformat()}_{right}_{strike}"
    return symbol, ticker, OptionSpec(right=right, strike=strike, expiry=expiry)


def _normalize_instrument(
    item: dict,
) -> tuple[AssetType, str, str, OptionSpec | None, float, float]:
    instrument = item.get("instrument") or {}
    asset_type_raw = str(instrument.get("assetType") or instrument.get("type") or "").upper()
    symbol_raw = str(instrument.get("symbol", "")).strip()
    underlying = str(instrument.get("underlyingSymbol") or symbol_raw).strip().upper()

    amount = _float_or_zero(item.get("amount"))
    price = _float_or_zero(item.get("price"))
    qty = abs(amount)

    if asset_type_raw in _EXCLUDED_ASSET_TYPES:
        raise ValueError(f"Unsupported asset type: {asset_type_raw}")

    if asset_type_raw == "OPTION" or "OPTION" in asset_type_raw:
        synthetic = from_occ(symbol_raw)
        if synthetic:
            symbol, underlying, option_spec = _option_spec_from_synthetic(synthetic)
            return AssetType.OPTION, symbol, underlying, option_spec, qty, price

        asset_type, symbol, underlying, option_spec = parse_option_from_text(symbol_raw, underlying)
        if asset_type == AssetType.OPTION and option_spec is not None:
            return asset_type, symbol, underlying, option_spec, qty, price
        raise ValueError(f"Unparseable option symbol: {symbol_raw}")

    if (
        asset_type_raw in {"EQUITY", "ETF", "COLLECTIVE_INVESTMENT", "INDEX", "MUTUAL_FUND"}
        or not asset_type_raw
    ):
        ticker = symbol_raw.split()[0].upper() if symbol_raw else underlying
        return AssetType.STOCK, ticker, ticker, None, qty, price

    raise ValueError(f"Unsupported asset type: {asset_type_raw}")


def _transaction_to_events(
    txn: dict,
    *,
    account: str,
    source: str,
    idx: int,
) -> list[TradeEvent]:
    txn_type = str(txn.get("type", "")).upper()
    if txn_type and txn_type not in _TRADE_TXN_TYPES:
        return []

    items = _trade_items(txn.get("transferItems") or [])
    if not items:
        return []

    ts = _parse_timestamp(txn.get("time") or txn.get("tradeDate"))
    fees = _fees_from_transfer_items(txn.get("transferItems") or [])
    fee_per_leg = fees / len(items) if items else 0.0
    activity_id = txn.get("activityId") or txn.get("transactionId") or idx
    txn_instruction = str(txn.get("instruction") or "")

    events: list[TradeEvent] = []
    for leg_idx, item in enumerate(items):
        amount = _float_or_zero(item.get("amount"))
        if amount == 0:
            continue

        instruction = str(item.get("instruction") or txn_instruction)
        side = _side_from_instruction(instruction, amount)
        asset_type, symbol, underlying, option_spec, qty, price = _normalize_instrument(item)

        silo = Silo.STOCK_OPTIONS

        events.append(
            TradeEvent(
                broker="schwab",
                account=account,
                timestamp=ts,
                symbol=symbol,
                underlying=underlying,
                asset_type=asset_type,
                side=side,
                quantity=qty,
                price=price,
                fees=fee_per_leg,
                option_spec=option_spec,
                silo=silo,
                raw_ref=f"{source}::txn_{activity_id}::leg_{leg_idx}",
                fill_id=f"{activity_id}::{leg_idx}",
            )
        )
    return events


def parse_transactions_response(
    data: list | dict,
    *,
    account: str,
    source: str = "schwab-api",
) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
    """Normalize Schwab get_transactions JSON to TradeEvents + review queue."""
    txns = data if isinstance(data, list) else data.get("transactions") or []
    events: list[TradeEvent] = []
    review: list[ReviewQueueItem] = []

    for idx, txn in enumerate(txns):
        try:
            events.extend(_transaction_to_events(txn, account=account, source=source, idx=idx))
        except Exception as exc:
            review.append(
                ReviewQueueItem(
                    source_file=source,
                    row_index=idx,
                    reason=str(exc),
                    raw_row=txn if isinstance(txn, dict) else {"raw": txn},
                )
            )

    events.sort(key=lambda e: e.timestamp)
    return events, review


def fetch_all(
    account: str = "schwab-tos",
    start: date | None = None,
    end: date | None = None,
    client: SchwabSession | None = None,
) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
    """Pull trade transactions and normalize to canonical TradeEvents."""
    session = client or get_client()
    account_hash = _account_hash_for_label(session, account)

    end = end or date.today()
    start = start or date(end.year - 1, end.month, end.day)

    txn_type = session.Transactions.TransactionType
    resp = session.get_transactions(
        account_hash,
        start_date=start,
        end_date=end,
        transaction_types=[txn_type.TRADE, txn_type.RECEIVE_AND_DELIVER],
    )
    if resp.status_code != httpx.codes.OK:
        raise RuntimeError(f"Schwab transactions failed: {resp.status_code} {resp.text[:200]}")

    return parse_transactions_response(resp.json(), account=account)
