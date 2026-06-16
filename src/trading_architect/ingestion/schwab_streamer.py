"""Schwab Streamer WebSocket client — LEVELONE_EQUITIES and LEVELONE_OPTIONS."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from trading_architect.ingestion.schwab_auth import (
    SchwabAuthExpired,
    StreamerInfo,
    get_client,
    get_user_preference,
)
from trading_architect.ingestion.schwab_symbols import from_occ, to_occ

logger = logging.getLogger(__name__)

EQUITY_FIELDS = "1,2,3,33,35"
OPTION_FIELDS = "2,3,4,9,10,20,21,27,28,29,30,31,32,34,37,39"

_RECONNECT_CODES = frozenset({3, 12, 20, 30})
_HEARTBEAT_STALE_SECONDS = 45.0
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0


def _is_benign_streamer_disconnect(exc: BaseException) -> bool:
    """Normal WebSocket close — reconnect without error-level logging."""
    try:
        from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
    except ImportError:
        return False
    return isinstance(exc, (ConnectionClosedOK, ConnectionClosed))


_client_lock = threading.Lock()
_shared_client: StreamerClient | None = None


def schwab_streamer_available() -> bool:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def _require_websockets() -> None:
    try:
        import websockets  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            'websockets is required for Schwab Streamer. Install with: pip install -e ".[schwab]"'
        ) from exc


def _float_field(row: dict[str, Any], key: str | int) -> float | None:
    val = row.get(str(key), row.get(key))
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


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


def _epoch_ms_to_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        ms = int(float(raw))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


@dataclass
class StreamerQuote:
    """Latest-value cache entry for one symbol."""

    symbol: str
    occ_key: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    iv: float | None = None
    strike: float | None = None
    right: str | None = None
    dte: int | None = None
    underlying_price: float | None = None
    delayed: bool = False
    asof: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_option: bool = False

    def preferred_price(self) -> float | None:
        for val in (self.mark, self.last):
            if val is not None:
                return val
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return self.bid if self.bid is not None else self.ask


def merge_quote_update(
    existing: StreamerQuote | None, row: dict[str, Any], *, is_option: bool
) -> StreamerQuote:
    """Merge a partial Streamer data row into the latest-value cache."""
    occ_key = str(row.get("key", ""))
    internal = from_occ(occ_key) or occ_key.upper()
    base = existing or StreamerQuote(symbol=internal, occ_key=occ_key, is_option=is_option)

    delayed = row.get("delayed", base.delayed)
    if isinstance(delayed, str):
        delayed = delayed.lower() in ("true", "1", "yes")
    delayed = bool(delayed)

    asof = base.asof
    if is_option:
        trade_time = _epoch_ms_to_datetime(row.get("39")) or _epoch_ms_to_datetime(row.get("38"))
    else:
        trade_time = _epoch_ms_to_datetime(row.get("35"))
    if trade_time is not None:
        asof = trade_time

    if is_option:
        return StreamerQuote(
            symbol=internal,
            occ_key=occ_key,
            bid=_float_field(row, 2) if 2 in row or "2" in row else base.bid,
            ask=_float_field(row, 3) if 3 in row or "3" in row else base.ask,
            last=_float_field(row, 4) if 4 in row or "4" in row else base.last,
            mark=_float_field(row, 37) if 37 in row or "37" in row else base.mark,
            delta=_float_field(row, 28) if 28 in row or "28" in row else base.delta,
            gamma=_float_field(row, 29) if 29 in row or "29" in row else base.gamma,
            theta=_float_field(row, 30) if 30 in row or "30" in row else base.theta,
            vega=_float_field(row, 31) if 31 in row or "31" in row else base.vega,
            rho=_float_field(row, 32) if 32 in row or "32" in row else base.rho,
            iv=_normalize_iv(row.get("10")) if 10 in row or "10" in row else base.iv,
            strike=_float_field(row, 20) if 20 in row or "20" in row else base.strike,
            right=str(row["21"]).upper() if "21" in row else base.right,
            dte=int(row["27"]) if "27" in row and row["27"] is not None else base.dte,
            underlying_price=_float_field(row, 35)
            if 35 in row or "35" in row
            else base.underlying_price,
            delayed=delayed,
            asof=asof,
            is_option=True,
        )

    return StreamerQuote(
        symbol=internal,
        occ_key=occ_key,
        bid=_float_field(row, 1) if 1 in row or "1" in row else base.bid,
        ask=_float_field(row, 2) if 2 in row or "2" in row else base.ask,
        last=_float_field(row, 3) if 3 in row or "3" in row else base.last,
        mark=_float_field(row, 33) if 33 in row or "33" in row else base.mark,
        delayed=delayed,
        asof=asof,
        is_option=False,
    )


def decode_equity_row(row: dict[str, Any]) -> StreamerQuote:
    return merge_quote_update(None, row, is_option=False)


def decode_option_row(row: dict[str, Any]) -> StreamerQuote:
    return merge_quote_update(None, row, is_option=True)


def process_streamer_message(
    payload: dict[str, Any],
    cache: dict[str, StreamerQuote],
    *,
    on_reconnect: Callable[[], None] | None = None,
) -> None:
    """Apply response/notify/data frames to the quote cache (testable seam)."""
    for item in payload.get("response") or []:
        content = item.get("content") or {}
        code = content.get("code")
        if code in _RECONNECT_CODES and on_reconnect is not None:
            on_reconnect()

    for block in payload.get("data") or []:
        service = block.get("service")
        is_option = service == "LEVELONE_OPTIONS"
        for row in block.get("content") or []:
            quote = merge_quote_update(
                cache.get(from_occ(str(row.get("key", ""))) or str(row.get("key", "")).upper()),
                row,
                is_option=is_option,
            )
            cache[quote.symbol] = quote


class StreamerClient:
    """Single process-wide Schwab Streamer connection with reconnect."""

    def __init__(
        self,
        *,
        session=None,
        streamer_info: StreamerInfo | None = None,
        ws_connect: Callable[..., Any] | None = None,
        access_token_fn: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        self._info = streamer_info
        self._ws_connect = ws_connect
        self._access_token_fn = access_token_fn or self._default_access_token
        self._cache: dict[str, StreamerQuote] = {}
        self._cache_lock = threading.Lock()
        self._equity_symbols: set[str] = set()
        self._option_symbols: set[str] = set()
        self._request_id = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._resubscribe = threading.Event()
        self._last_heartbeat = time.monotonic()
        self._ws: Any = None

    @staticmethod
    def _default_access_token() -> str:
        from trading_architect.ingestion.schwab_auth import _load_token_bundle

        get_client()
        bundle = _load_token_bundle()
        if not bundle:
            raise SchwabAuthExpired()
        token = bundle.get("token", bundle)
        access = token.get("access_token") if isinstance(token, dict) else None
        if not access:
            raise SchwabAuthExpired()
        return str(access)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def connect(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        _require_websockets()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="schwab-streamer", daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=15.0):
            raise TimeoutError("Schwab Streamer failed to connect within 15s")

    def subscribe(self, equities: list[str], options: list[str]) -> None:
        self._equity_symbols = {s.upper() for s in equities if s and "_" not in s}
        self._option_symbols = {to_occ(s) for s in options if s}
        if not self.is_connected():
            self.connect()
        self._resubscribe.set()

    def latest(self, symbol: str) -> StreamerQuote | None:
        key = symbol if "_" not in symbol else symbol
        with self._cache_lock:
            if key in self._cache:
                return self._cache[key]
            occ = to_occ(symbol)
            internal = from_occ(occ) or symbol.upper()
            return self._cache.get(internal) or self._cache.get(symbol.upper())

    def close(self) -> None:
        self._stop.set()
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send_logout(), self._loop)
        if self._thread:
            self._thread.join(timeout=5.0)
        self._connected.clear()
        with _client_lock:
            global _shared_client
            if _shared_client is self:
                _shared_client = None

    def _next_request_id(self) -> str:
        self._request_id += 1
        return str(self._request_id)

    def _run_loop(self) -> None:
        global _shared_client
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        backoff = _BACKOFF_INITIAL
        while not self._stop.is_set():
            try:
                self._loop.run_until_complete(self._session_main())
                backoff = _BACKOFF_INITIAL
            except SchwabAuthExpired:
                logger.warning(
                    "Schwab Streamer stopped: authentication expired. "
                    "Reconnect with `ta fetch-schwab --login` or reauthenticate in Settings."
                )
                self._connected.clear()
                self._session = None
                with _client_lock:
                    if _shared_client is self:
                        _shared_client = None
                break
            except Exception as exc:
                self._connected.clear()
                if self._stop.is_set():
                    break
                if _is_benign_streamer_disconnect(exc):
                    logger.info(
                        "Schwab Streamer disconnected (%s); reconnecting in %.0fs",
                        exc,
                        backoff,
                    )
                else:
                    logger.exception("Schwab Streamer session ended")
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _session_main(self) -> None:
        info = self._info or get_user_preference(client=self._session or get_client())
        ws_connect = self._ws_connect
        if ws_connect is None:
            import websockets

            ws_connect = websockets.connect

        async with ws_connect(info.streamer_socket_url) as ws:
            self._ws = ws
            await self._login(ws, info)
            self._connected.set()
            await self._send_subscriptions(ws, info)
            while not self._stop.is_set():
                if self._resubscribe.is_set():
                    self._resubscribe.clear()
                    await self._send_subscriptions(ws, info)
                if time.monotonic() - self._last_heartbeat > _HEARTBEAT_STALE_SECONDS:
                    raise ConnectionError("Schwab Streamer heartbeat stale")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except TimeoutError:
                    continue
                self._handle_raw_message(raw)

    async def _login(self, ws: Any, info: StreamerInfo) -> None:
        token = self._access_token_fn()
        req = {
            "requests": [
                {
                    "requestid": self._next_request_id(),
                    "service": "ADMIN",
                    "command": "LOGIN",
                    "SchwabClientCustomerId": info.schwab_client_customer_id,
                    "SchwabClientCorrelId": info.schwab_client_correl_id,
                    "parameters": {
                        "Authorization": token,
                        "SchwabClientChannel": info.schwab_client_channel,
                        "SchwabClientFunctionId": info.schwab_client_function_id,
                    },
                }
            ]
        }
        await ws.send(json.dumps(req))
        raw = await ws.recv()
        payload = json.loads(raw)
        for item in payload.get("response") or []:
            content = item.get("content") or {}
            if content.get("code") != 0:
                raise SchwabAuthExpired(content.get("msg", "Streamer LOGIN failed"))

    async def _send_subscriptions(self, ws: Any, info: StreamerInfo) -> None:
        requests: list[dict[str, Any]] = []
        if self._equity_symbols:
            requests.append(
                {
                    "requestid": self._next_request_id(),
                    "service": "LEVELONE_EQUITIES",
                    "command": "SUBS",
                    "SchwabClientCustomerId": info.schwab_client_customer_id,
                    "SchwabClientCorrelId": info.schwab_client_correl_id,
                    "parameters": {
                        "keys": ",".join(sorted(self._equity_symbols)),
                        "fields": EQUITY_FIELDS,
                    },
                }
            )
        if self._option_symbols:
            requests.append(
                {
                    "requestid": self._next_request_id(),
                    "service": "LEVELONE_OPTIONS",
                    "command": "SUBS",
                    "SchwabClientCustomerId": info.schwab_client_customer_id,
                    "SchwabClientCorrelId": info.schwab_client_correl_id,
                    "parameters": {
                        "keys": ",".join(sorted(self._option_symbols)),
                        "fields": OPTION_FIELDS,
                    },
                }
            )
        if requests:
            await ws.send(json.dumps({"requests": requests}))

    async def _send_logout(self) -> None:
        if not self._ws:
            return
        info = self._info or get_user_preference(client=self._session or get_client())
        req = {
            "requests": [
                {
                    "requestid": self._next_request_id(),
                    "service": "ADMIN",
                    "command": "LOGOUT",
                    "SchwabClientCustomerId": info.schwab_client_customer_id,
                    "SchwabClientCorrelId": info.schwab_client_correl_id,
                    "parameters": {},
                }
            ]
        }
        try:
            await self._ws.send(json.dumps(req))
        except Exception:
            pass

    def _handle_raw_message(self, raw: str) -> None:
        payload = json.loads(raw)
        if payload.get("notify"):
            self._last_heartbeat = time.monotonic()
        reconnect = False

        def trigger() -> None:
            nonlocal reconnect
            reconnect = True

        with self._cache_lock:
            process_streamer_message(
                payload,
                self._cache,
                on_reconnect=trigger,
            )
        if reconnect:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        self._connected.clear()
        self._resubscribe.set()
        if self._loop and self._loop.is_running() and self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)


def reset_streamer_client() -> None:
    """Close and discard the process-wide streamer singleton."""
    global _shared_client
    with _client_lock:
        if _shared_client is not None:
            _shared_client.close()
            _shared_client = None


def get_streamer_client(**kwargs: Any) -> StreamerClient:
    """Return the process-wide StreamerClient singleton."""
    global _shared_client
    with _client_lock:
        if _shared_client is None:
            _shared_client = StreamerClient(**kwargs)
        return _shared_client
