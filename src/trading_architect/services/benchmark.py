"""Benchmark price capture — SPY daily close via Schwab quotes."""

from __future__ import annotations

from datetime import date, datetime, timezone

from trading_architect.config.env import schwab_credentials_configured
from trading_architect.ingestion.schwab_auth import SchwabAuthExpired, schwab_py_available
from trading_architect.store.repository import BenchmarkPriceRecord, Repository

BENCHMARK_SYMBOL = "SPY"


def _float_or_none(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def parse_fundamental_earnings(data: dict, underlying: str) -> date | None:
    """Extract next earnings date from Schwab quote/fundamental payload when present."""
    block = data.get(underlying.upper()) or data.get(underlying) or {}
    fundamental = block.get("fundamental") or {}
    for key in ("nextEarningsDate", "nextDividendExDate", "divExDate"):
        raw = fundamental.get(key)
        if not raw:
            continue
        try:
            if isinstance(raw, str):
                if "T" in raw:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
                return date.fromisoformat(raw[:10])
        except ValueError:
            continue
    return None


def parse_spy_close_from_quotes(data: dict) -> float | None:
    """Extract SPY close/mark from Schwab GET /quotes response."""
    block = data.get(BENCHMARK_SYMBOL) or {}
    quote = block.get("quote") or block
    for key in ("closePrice", "mark", "lastPrice", "regularMarketLastPrice"):
        price = _float_or_none(quote.get(key))
        if price is not None:
            return price
    return None


def fetch_spy_close(*, client=None) -> float | None:
    """Fetch current SPY price from Schwab; returns None when unavailable."""
    from trading_architect.ingestion.schwab_auth import get_client
    from trading_architect.ingestion.schwab_rest import _raise_for_response

    client = get_client(client=client)
    resp = client.get_quotes([BENCHMARK_SYMBOL])
    data = _raise_for_response(resp, f"quotes for {BENCHMARK_SYMBOL}")
    return parse_spy_close_from_quotes(data)


def fetch_earnings_date_schwab(underlying: str, *, client=None) -> date | None:
    """Try Schwab fundamentals block for next earnings date."""
    from trading_architect.ingestion.schwab_auth import get_client
    from trading_architect.ingestion.schwab_rest import _raise_for_response

    client = get_client(client=client)
    sym = underlying.upper()
    getter = getattr(client, "get_quotes", None)
    if getter is None:
        return None
    try:
        resp = getter([sym], fields="quote,fundamental")
    except TypeError:
        resp = getter([sym])
    try:
        data = _raise_for_response(resp, f"fundamentals for {sym}")
    except Exception:
        return None
    return parse_fundamental_earnings(data, sym)


def record_daily_benchmark(
    repo: Repository,
    *,
    price_date: date | None = None,
    client=None,
) -> BenchmarkPriceRecord | None:
    """Persist one SPY close for today (or price_date) if not already stored."""
    d = price_date or datetime.now(timezone.utc).date()
    if repo.has_benchmark_price_for_date(BENCHMARK_SYMBOL, d):
        return repo.get_benchmark_price(BENCHMARK_SYMBOL, d)

    if not schwab_py_available() or not schwab_credentials_configured():
        return None

    try:
        close = fetch_spy_close(client=client)
    except SchwabAuthExpired:
        return None
    except Exception:
        return None

    if close is None:
        return None

    record = BenchmarkPriceRecord(
        symbol=BENCHMARK_SYMBOL,
        price_date=d,
        close_price=close,
        source="schwab",
    )
    return repo.record_benchmark_price(record)
