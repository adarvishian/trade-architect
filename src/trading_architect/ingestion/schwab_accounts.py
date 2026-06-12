"""Schwab account hash resolution, balances, and positions snapshot (REST)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from trading_architect.config.user_settings import AppSettings
from trading_architect.ingestion.parsing import parse_option_from_text
from trading_architect.ingestion.robinhood_fetch import consolidate_holdings
from trading_architect.ingestion.schwab_auth import SchwabSession, get_client
from trading_architect.ingestion.schwab_symbols import from_occ as occ_to_synthetic
from trading_architect.ingestion.schwab_symbols import parse_synthetic_option
from trading_architect.models.entities import (
    AssetType,
    BrokerAccountBalance,
    HoldingLeg,
    SchwabAccountSnapshot,
)
from trading_architect.store.repository import Repository

_EXCLUDED_ASSET_TYPES = frozenset({"FUTURE", "FUTURES", "FOREX"})


def _float_or_zero(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def redact_account_number(account_number: str) -> str:
    """Mask plain account numbers for display and logs (FR-ACCT-1)."""
    text = str(account_number or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    return f"…{text[-4:]}"


def redact_account_display(label: str, account_number: str | None = None) -> str:
    """Human-readable account reference without exposing the full number."""
    if account_number:
        return f"{label} ({redact_account_number(account_number)})"
    return label


def _securities_account(payload: dict) -> dict:
    if "securitiesAccount" in payload:
        return payload["securitiesAccount"] or {}
    return payload


def _asset_type_from_instrument(instrument: dict) -> AssetType | None:
    raw = str(instrument.get("assetType") or instrument.get("type") or "").upper()
    if raw in _EXCLUDED_ASSET_TYPES:
        return None
    if raw in {"OPTION"} or "OPTION" in raw:
        return AssetType.OPTION
    if raw in {"EQUITY", "ETF", "COLLECTIVE_INVESTMENT", "INDEX", "MUTUAL_FUND"}:
        return AssetType.STOCK
    if raw in {"SWEEP_VEHICLE", "CASH_EQUIVALENT", "BOND", "FIXED_INCOME"}:
        return None
    if raw:
        return AssetType.STOCK
    return None


def _holding_from_position(position: dict, *, account: str) -> HoldingLeg | None:
    instrument = position.get("instrument") or {}
    asset_type = _asset_type_from_instrument(instrument)
    if asset_type is None:
        return None

    long_qty = _float_or_zero(position.get("longQuantity"))
    short_qty = _float_or_zero(position.get("shortQuantity"))
    quantity = long_qty - short_qty
    if quantity == 0:
        return None

    symbol_raw = str(instrument.get("symbol", "")).strip()
    underlying = str(instrument.get("underlyingSymbol") or symbol_raw).strip().upper()

    if asset_type == AssetType.OPTION:
        symbol = symbol_raw
        synthetic = occ_to_synthetic(symbol_raw)
        if synthetic:
            parsed = parse_synthetic_option(synthetic)
            if parsed:
                underlying, symbol, _ = parsed
            else:
                symbol = synthetic
        else:
            desc = str(instrument.get("description") or "")
            if desc:
                parsed_type, sym, und, _spec = parse_option_from_text(desc, underlying)
                if parsed_type == AssetType.OPTION and _spec:
                    underlying, symbol = und, sym
    else:
        symbol = symbol_raw.split()[0].upper() if symbol_raw else underlying
        underlying = symbol

    avg = _float_or_zero(position.get("averagePrice"))
    if avg == 0:
        if quantity > 0:
            avg = _float_or_zero(position.get("averageLongPrice"))
        else:
            avg = _float_or_zero(position.get("averageShortPrice"))

    return HoldingLeg(
        account=account,
        symbol=symbol,
        underlying=underlying,
        asset_type=asset_type,
        quantity=quantity,
        average_cost=avg,
        market_value=_float_or_zero(position.get("marketValue")),
    )


def parse_account_balance(
    payload: dict,
    *,
    label: str,
) -> BrokerAccountBalance:
    """Map GET /accounts/{hash} JSON to BrokerAccountBalance."""
    acct = _securities_account(payload)
    initial = acct.get("initialBalances") or {}
    current = acct.get("currentBalances") or {}

    cash = _float_or_zero(current.get("cashBalance") or initial.get("cashBalance"))
    mmf = _float_or_zero(current.get("moneyMarketFund") or initial.get("moneyMarketFund"))
    total_cash = _float_or_zero(current.get("totalCash") or initial.get("totalCash"))
    if total_cash <= 0:
        total_cash = cash + mmf
    equivalents = max(0.0, total_cash - cash)

    liquidation = _float_or_zero(current.get("liquidationValue"))
    equity = _float_or_zero(current.get("equity"))
    portfolio_equity = liquidation if liquidation else equity

    account_type = str(acct.get("type") or "")

    return BrokerAccountBalance(
        account=label,
        account_type=account_type,
        cash=cash,
        cash_equivalents=equivalents,
        cash_and_equivalents=total_cash,
        portfolio_equity=portfolio_equity,
        buying_power=_float_or_zero(current.get("buyingPower") or initial.get("buyingPower")),
    )


def parse_holdings(payload: dict, *, label: str) -> list[HoldingLeg]:
    """Parse positions[] from an account payload; futures excluded."""
    acct = _securities_account(payload)
    legs: list[HoldingLeg] = []
    for position in acct.get("positions") or []:
        leg = _holding_from_position(position, account=label)
        if leg:
            legs.append(leg)
    return legs


def _account_fields(client: SchwabSession) -> Any:
    """schwab-py requires Account.Fields.POSITIONS enum, not a raw string."""
    return client.Account.Fields.POSITIONS


def _fetch_account_payload(client: SchwabSession, account_hash: str) -> dict:
    resp = client.get_account(account_hash, fields=_account_fields(client))
    if resp.status_code != httpx.codes.OK:
        raise RuntimeError(
            f"Schwab account fetch failed: {resp.status_code} {getattr(resp, 'text', '')[:200]}"
        )
    data = resp.json()
    if isinstance(data, list):
        if not data:
            raise RuntimeError("Schwab account response was empty.")
        first = data[0]
        return first if isinstance(first, dict) else {}
    return data if isinstance(data, dict) else {}


def _account_numbers_from_api(client: SchwabSession) -> list[dict]:
    resp = client.get_account_numbers()
    if resp.status_code != httpx.codes.OK:
        raise RuntimeError(f"Failed to list Schwab accounts: {resp.status_code}")
    return list(resp.json() or [])


def refresh_schwab_account_mappings(
    client: SchwabSession,
    settings: AppSettings,
) -> AppSettings:
    """Merge API account hashes into settings; assign default labels for new accounts."""
    api_accounts = _account_numbers_from_api(client)
    hashes = dict(settings.schwab_account_hashes)
    known_hashes = set(hashes.values())

    default_labels = ["schwab-tos", "schwab-ira", "schwab-2", "schwab-3"]
    label_idx = 0

    for acct in api_accounts:
        hash_val = str(acct.get("hashValue", ""))
        if not hash_val or hash_val in known_hashes:
            continue
        while label_idx < len(default_labels) and default_labels[label_idx] in hashes:
            label_idx += 1
        label = (
            default_labels[label_idx]
            if label_idx < len(default_labels)
            else f"schwab-{len(hashes) + 1}"
        )
        hashes[label] = hash_val
        known_hashes.add(hash_val)
        label_idx += 1

    if len(api_accounts) == 1 and not hashes:
        hash_val = str(api_accounts[0].get("hashValue", ""))
        if hash_val:
            hashes["schwab-tos"] = hash_val

    settings.schwab_account_hashes = hashes
    return settings


def list_accounts(
    client: SchwabSession | None = None,
    *,
    repo: Repository | None = None,
) -> list[tuple[str, str]]:
    """Return (label, hash) pairs from persisted settings, refreshed against the API."""
    client = client or get_client()
    if repo is not None:
        settings = repo.load_app_settings()
        settings = refresh_schwab_account_mappings(client, settings)
        repo.save_app_settings(settings)
        return sorted(settings.schwab_account_hashes.items())

    settings = refresh_schwab_account_mappings(client, AppSettings())
    return sorted(settings.schwab_account_hashes.items())


def _account_hash_for_label(
    client: SchwabSession,
    label: str,
    *,
    account_hashes: dict[str, str] | None = None,
) -> str:
    label_lower = label.lower()
    if account_hashes:
        if label in account_hashes:
            return account_hashes[label]
        for lbl, hash_val in account_hashes.items():
            if lbl.lower() == label_lower:
                return hash_val

    if label and len(label) > 20 and " " not in label:
        return label

    accounts = _account_numbers_from_api(client)
    for acct in accounts:
        number = str(acct.get("accountNumber", ""))
        hash_val = str(acct.get("hashValue", ""))
        if not hash_val:
            continue
        if label == hash_val:
            return hash_val
        if label_lower in number.lower() or label_lower in hash_val.lower():
            return hash_val

    if len(accounts) == 1:
        return str(accounts[0]["hashValue"])

    available = [
        redact_account_display("account", str(a.get("accountNumber", "?"))) for a in accounts
    ]
    raise ValueError(
        f"No Schwab account matched '{label}'. Available: {', '.join(available) or 'none'}"
    )


def fetch_account_balance(
    label: str,
    client: SchwabSession | None = None,
    *,
    repo: Repository | None = None,
) -> BrokerAccountBalance:
    client = client or get_client()
    hashes = None
    if repo is not None:
        settings = repo.load_app_settings()
        settings = refresh_schwab_account_mappings(client, settings)
        repo.save_app_settings(settings)
        hashes = settings.schwab_account_hashes
    account_hash = _account_hash_for_label(client, label, account_hashes=hashes)
    payload = _fetch_account_payload(client, account_hash)
    return parse_account_balance(payload, label=label)


def fetch_holdings(
    label: str,
    client: SchwabSession | None = None,
    *,
    repo: Repository | None = None,
) -> list[HoldingLeg]:
    client = client or get_client()
    hashes = None
    if repo is not None:
        settings = repo.load_app_settings()
        hashes = settings.schwab_account_hashes
    account_hash = _account_hash_for_label(client, label, account_hashes=hashes)
    payload = _fetch_account_payload(client, account_hash)
    return parse_holdings(payload, label=label)


def fetch_portfolio_snapshot_with_legs(
    labels: list[str] | None = None,
    client: SchwabSession | None = None,
    *,
    repo: Repository | None = None,
    persist: bool = True,
) -> tuple[SchwabAccountSnapshot, list[HoldingLeg]]:
    """Fetch balances and open holdings; optionally persist snapshots to the store."""
    client = client or get_client()
    if repo is not None:
        settings = repo.load_app_settings()
        settings = refresh_schwab_account_mappings(client, settings)
        repo.save_app_settings(settings)
        linked = list(settings.schwab_account_hashes.items())
    else:
        linked = list_accounts(client)

    if labels:
        wanted = {label.lower() for label in labels}
        linked = [(lbl, h) for lbl, h in linked if lbl.lower() in wanted]
        if not linked:
            raise ValueError(f"No Schwab account labels matched: {', '.join(labels)}")

    balances: list[BrokerAccountBalance] = []
    all_legs: list[HoldingLeg] = []
    for label, account_hash in linked:
        payload = _fetch_account_payload(client, account_hash)
        balances.append(parse_account_balance(payload, label=label))
        all_legs.extend(parse_holdings(payload, label=label))

    snapshot = SchwabAccountSnapshot(
        fetched_at=datetime.now(timezone.utc),
        accounts=balances,
        holdings=consolidate_holdings(all_legs),
    )

    if persist and repo is not None:
        from trading_architect.models.entities import Silo
        from trading_architect.services.snapshot_persist import persist_legs_snapshot

        persist_legs_snapshot(
            repo,
            kind="schwab",
            silo=Silo.STOCK_OPTIONS,
            institution="Schwab",
            fetched_at=snapshot.fetched_at,
            accounts=balances,
            legs=all_legs,
        )

    return snapshot, all_legs


def fetch_portfolio_snapshot(
    labels: list[str] | None = None,
    client: SchwabSession | None = None,
    *,
    repo: Repository | None = None,
) -> SchwabAccountSnapshot:
    """Fetch balances and open holdings for one or more labeled Schwab accounts."""
    snapshot, _legs = fetch_portfolio_snapshot_with_legs(
        labels,
        client=client,
        repo=repo,
        persist=repo is not None,
    )
    return snapshot
