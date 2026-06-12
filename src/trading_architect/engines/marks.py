"""Marked-to-market price provider — PRD A.6 / Schwab adapter §7."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from trading_architect.config.defaults import DEFAULT_IV_FALLBACK

_OPTION_SYMBOL = re.compile(
    r"^(?P<ticker>[A-Z]{1,6})_(?P<exp>\d{4}-\d{2}-\d{2})_(?P<right>[CP])_(?P<strike>[\d.]+)$"
)


@dataclass(frozen=True)
class Mark:
    symbol: str
    price: float
    delta: float | None
    asof: datetime
    delayed: bool = False
    iv_fallback: bool = False
    theta: float | None = None


@runtime_checkable
class MarksProvider(Protocol):
    def marks_for(self, symbols: list[str]) -> dict[str, Mark]: ...


class NullMarksProvider:
    """Realized-only mode — no live marks."""

    def marks_for(self, symbols: list[str]) -> dict[str, Mark]:
        return {}


class StaticMarksProvider:
    """Manual or CSV-sourced marks for offline use."""

    def __init__(self, marks: dict[str, Mark]) -> None:
        self._marks = marks

    def marks_for(self, symbols: list[str]) -> dict[str, Mark]:
        return {s: self._marks[s] for s in symbols if s in self._marks}


def _is_option_symbol(symbol: str) -> bool:
    return bool(_OPTION_SYMBOL.match(symbol.strip().upper()))


def _parse_option(symbol: str) -> tuple[str, date, str, float] | None:
    match = _OPTION_SYMBOL.match(symbol.strip().upper())
    if not match:
        return None
    return (
        match.group("ticker"),
        date.fromisoformat(match.group("exp")),
        match.group("right").upper(),
        float(match.group("strike")),
    )


def _naive_dt(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _delta_with_fallback(
    *,
    symbol: str,
    delta: float | None,
    spot: float | None,
    iv: float | None,
    strike: float | None,
    right: str | None,
    dte: int | None,
) -> tuple[float | None, bool]:
    if delta is not None:
        return delta, False
    parsed = _parse_option(symbol)
    if not parsed or spot is None or spot <= 0:
        return None, False
    _, expiry, opt_right, opt_strike = parsed
    use_strike = strike if strike is not None else opt_strike
    use_right = right or opt_right
    if dte is not None and dte >= 0:
        time_years = max(dte / 365.0, 1 / 365.0)
    else:
        time_years = max((expiry - date.today()).days / 365.0, 1 / 365.0)
    iv_fallback = not (iv and iv > 0)
    vol = iv if iv and iv > 0 else DEFAULT_IV_FALLBACK
    from trading_architect.engines.options_pricing import bs_greeks

    return bs_greeks(spot, use_strike, time_years, 0.05, vol, use_right)["delta"], iv_fallback


class SchwabMarksProvider:
    """Live marks: Streamer-first with REST /quotes fallback."""

    def __init__(
        self,
        client=None,
        streamer=None,
        *,
        ttl_seconds: float = 120.0,
        use_streamer: bool = True,
    ) -> None:
        self._client = client
        self._streamer = streamer
        self._ttl = ttl_seconds
        self._use_streamer = use_streamer
        self._cache: dict[str, tuple[float, Mark]] = {}

    def _get_streamer(self):
        if not self._use_streamer:
            return None
        if self._streamer is not None:
            return self._streamer
        from trading_architect.ingestion.schwab_streamer import (
            get_streamer_client,
            schwab_streamer_available,
        )

        if not schwab_streamer_available():
            return None
        return get_streamer_client(session=self._client)

    def marks_for(self, symbols: list[str]) -> dict[str, Mark]:
        if not symbols:
            return {}

        equities = [s for s in symbols if not _is_option_symbol(s)]
        options = [s for s in symbols if _is_option_symbol(s)]

        streamer = self._get_streamer()
        if streamer is not None:
            try:
                streamer.subscribe(equities, options)
            except Exception:
                streamer = None

        result: dict[str, Mark] = {}
        missing: list[str] = []

        for sym in symbols:
            quote = None
            if streamer is not None:
                try:
                    quote = streamer.latest(sym)
                except Exception:
                    quote = None
            if quote is not None and quote.preferred_price() is not None:
                result[sym] = self._mark_from_quote(quote, result)
            else:
                missing.append(sym)

        if missing:
            rest_marks = self._fetch_rest(missing)
            for sym, mark in rest_marks.items():
                if sym not in result:
                    result[sym] = mark

        now = time.monotonic()
        for sym, mark in result.items():
            self._cache[sym] = (now, mark)

        return {s: result[s] for s in symbols if s in result}

    def _mark_from_quote(self, quote, existing: dict[str, Mark]) -> Mark:
        spot = quote.underlying_price
        if spot is None and quote.is_option:
            parsed = _parse_option(quote.symbol)
            if parsed:
                underlying = parsed[0]
                for key in (underlying, quote.symbol):
                    if key in existing:
                        spot = existing[key].price
                        break
                if streamer_underlying := self._underlying_spot_from_cache(underlying):
                    spot = streamer_underlying

        delta, iv_fallback = _delta_with_fallback(
            symbol=quote.symbol,
            delta=quote.delta,
            spot=spot,
            iv=quote.iv,
            strike=quote.strike,
            right=quote.right,
            dte=quote.dte,
        )
        price = quote.preferred_price()
        assert price is not None
        return Mark(
            symbol=quote.symbol,
            price=price,
            delta=delta,
            asof=_naive_dt(quote.asof),
            delayed=quote.delayed,
            iv_fallback=iv_fallback,
            theta=quote.theta,
        )

    def _underlying_spot_from_cache(self, underlying: str) -> float | None:
        cached = self._cache.get(underlying)
        if cached:
            return cached[1].price
        return None

    def _fetch_rest(self, symbols: list[str]) -> dict[str, Mark]:
        from trading_architect.ingestion.schwab_rest import fetch_quotes

        now = time.monotonic()
        stale = [s for s in symbols if s not in self._cache or now - self._cache[s][0] > self._ttl]
        if not stale:
            return {s: self._cache[s][1] for s in symbols if s in self._cache}

        try:
            fetched = fetch_quotes(stale, client=self._client)
        except Exception:
            return {s: self._cache[s][1] for s in symbols if s in self._cache}

        out: dict[str, Mark] = {}
        spot_by_underlying: dict[str, float] = {}
        for sym, mark in fetched.items():
            if not _is_option_symbol(sym):
                spot_by_underlying[sym.upper()] = mark.price

        for sym, mark in fetched.items():
            delta, iv_fallback = _delta_with_fallback(
                symbol=sym,
                delta=mark.delta,
                spot=spot_by_underlying.get(sym.split("_")[0]) if _is_option_symbol(sym) else None,
                iv=None,
                strike=None,
                right=None,
                dte=None,
            )
            out[sym] = Mark(
                symbol=sym,
                price=mark.price,
                delta=delta,
                asof=mark.asof,
                iv_fallback=iv_fallback,
                theta=getattr(mark, "theta", None),
            )
        return out


def default_marks_provider() -> MarksProvider:
    """Return Schwab provider when configured, else Null."""
    from trading_architect.config.env import schwab_credentials_configured
    from trading_architect.ingestion.schwab_auth import schwab_py_available, token_file_present

    if schwab_py_available() and schwab_credentials_configured() and token_file_present():
        return SchwabMarksProvider()
    return NullMarksProvider()
