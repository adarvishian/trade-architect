"""Shared parsing helpers for broker CSV adapters."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from trading_architect.models.entities import AssetType, OptionSpec, ReviewQueueItem


def parse_money(value) -> float:
    """Parse broker price/amount strings: $2.43, ($6.03), -$289.50, 1,234.56."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        raise ValueError("Missing numeric value")
    text = str(value).strip()
    if not text or text.lower() == "nan":
        raise ValueError("Empty numeric value")

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    text = text.replace("$", "").replace(",", "").strip()
    if text.startswith("-"):
        negative = True
        text = text[1:]

    result = float(text)
    return -result if negative else result


def parse_quantity(value) -> float:
    """Parse quantity, stripping suffixes like Robinhood's '1S' on expirations."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    text = str(value).strip().upper()
    if not text or text == "NAN":
        return 0.0
    text = re.sub(r"[A-Z]+$", "", text)  # trailing S on OEXP qty
    text = text.replace(",", "")
    return abs(float(text))


def normalize_timestamp(value: datetime) -> datetime:
    """Ensure UTC-aware datetimes so mixed broker imports can be sorted."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_timestamp(value) -> datetime:
    """Parse date/datetime, handling Schwab's 'MM/DD/YYYY as of MM/DD/YYYY'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        raise ValueError("Missing date")
    text = str(value).strip()
    if " as of " in text.lower():
        text = text.split(" as of ")[0].strip()
    return normalize_timestamp(pd.to_datetime(text).to_pydatetime())


def _review_malformed_line(
    source_file: str,
    row_index: int,
    fields: list[str],
    expected_fields: int,
    *,
    reason: str | None = None,
) -> ReviewQueueItem:
    detail = reason or f"malformed CSV line ({len(fields)} fields, expected {expected_fields})"
    return ReviewQueueItem(
        source_file=source_file,
        row_index=row_index,
        reason=detail,
        raw_row={"raw_line": ",".join(fields)},
    )


def _pandas_fallback_with_review(path: Path) -> tuple[pd.DataFrame, list[ReviewQueueItem]]:
    """Parse CSV via pandas when DictReader yields no rows; route bad lines to review."""
    source = Path(path).name
    review: list[ReviewQueueItem] = []

    with open(path, newline="", encoding="utf-8-sig") as handle:
        text = handle.read()

    if not text.strip():
        return pd.DataFrame(), review

    try:
        df = pd.read_csv(io.StringIO(text), engine="python")
        return df, review
    except pd.errors.ParserError:
        pass

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return pd.DataFrame(), review

    expected = len(header)
    good_rows: list[dict[str, str]] = []
    for line_num, fields in enumerate(reader, start=2):
        if not fields or all(not f.strip() for f in fields):
            continue
        if len(fields) != expected:
            review.append(
                _review_malformed_line(source, line_num, fields, expected),
            )
            continue
        good_rows.append(dict(zip(header, fields, strict=False)))

    return pd.DataFrame(good_rows), review


def read_broker_csv(path) -> tuple[pd.DataFrame, list[ReviewQueueItem]]:
    """Read broker CSV with tolerant quoting for multiline Robinhood fields."""
    source = Path(path).name
    review: list[ReviewQueueItem] = []

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            if not row:
                continue
            # Drop footer/disclaimer rows and empty lines
            first_val = next((v for v in row.values() if v and str(v).strip()), "")
            if not first_val or "informational purposes only" in str(first_val).lower():
                continue
            rows.append(row)
    if not rows:
        return _pandas_fallback_with_review(path)
    return pd.DataFrame(rows), review


OPTION_DESC_PATTERN = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})\s+"
    r"(?P<exp>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<right>Call|Put)\s+\$"
    r"(?P<strike>[\d.]+)$",
    re.IGNORECASE,
)

SCHWAB_OPTION_PATTERN = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})\s+"
    r"(?P<exp>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<strike>[\d.]+)\s+"
    r"(?P<right>[CP])$",
    re.IGNORECASE,
)


def parse_option_from_text(
    text: str, underlying_fallback: str | None = None
) -> tuple[AssetType, str, str, OptionSpec | None]:
    text = " ".join(text.split())  # collapse whitespace/newlines

    match = OPTION_DESC_PATTERN.match(text)
    if match:
        ticker = match.group("ticker").upper()
        expiry = pd.to_datetime(match.group("exp")).date()
        right = "C" if match.group("right").lower() == "call" else "P"
        strike = float(match.group("strike"))
        symbol = f"{ticker}_{expiry.isoformat()}_{right}_{strike}"
        return (
            AssetType.OPTION,
            symbol,
            ticker,
            OptionSpec(right=right, strike=strike, expiry=expiry),
        )

    match = SCHWAB_OPTION_PATTERN.match(text)
    if match:
        ticker = match.group("ticker").upper()
        expiry = pd.to_datetime(match.group("exp")).date()
        right = match.group("right").upper()
        strike = float(match.group("strike"))
        symbol = f"{ticker}_{expiry.isoformat()}_{right}_{strike}"
        return (
            AssetType.OPTION,
            symbol,
            ticker,
            OptionSpec(right=right, strike=strike, expiry=expiry),
        )

    ticker = (underlying_fallback or text.split()[0]).upper()
    return AssetType.STOCK, ticker, ticker, None


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


def normalize_ticker(value) -> str:
    if value is None:
        return ""
    text = str(value).strip().upper()
    if not text or text in {"NONE", "NAN"}:
        return ""
    return text


def is_valid_equity_ticker(ticker: str) -> bool:
    """True for normal US equity tickers; rejects blank/internal RH instruments."""
    normalized = normalize_ticker(ticker)
    return bool(normalized and _TICKER_RE.fullmatch(normalized))


def is_assemblable_event(event) -> bool:
    """Whether a trade event should participate in position assembly."""
    from trading_architect.models.entities import Silo

    underlying = normalize_ticker(getattr(event, "underlying", ""))
    if not underlying:
        return False
    if getattr(event, "silo", None) == Silo.FUTURES:
        return True
    return is_valid_equity_ticker(underlying)
