"""Tests for Schwab Streamer decode and reconnect behavior."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_architect.ingestion.schwab_auth import StreamerInfo
from trading_architect.ingestion.schwab_streamer import (
    StreamerClient,
    decode_equity_row,
    decode_option_row,
    merge_quote_update,
    process_streamer_message,
)

FIXTURES = Path(__file__).parent / "fixtures" / "schwab"

STREAMER_INFO = StreamerInfo(
    streamer_socket_url="wss://streamer.example.test/ws",
    schwab_client_customer_id="Customer1",
    schwab_client_correl_id="correl-abc-123",
    schwab_client_channel="N9",
    schwab_client_function_id="APIAPP",
)


def test_decode_equity_fixture():
    payload = json.loads((FIXTURES / "streamer_equity.json").read_text())
    row = payload["data"][0]["content"][0]
    quote = decode_equity_row(row)
    assert quote.symbol == "AAPL"
    assert quote.bid == pytest.approx(183.75)
    assert quote.ask == pytest.approx(183.8)
    assert quote.last == pytest.approx(183.78)
    assert quote.mark == pytest.approx(183.77)
    assert quote.delayed is False
    assert quote.preferred_price() == pytest.approx(183.77)
    assert quote.asof == datetime.fromtimestamp(1668715930.570, tz=timezone.utc)


def test_decode_option_fixture_iv_normalized():
    payload = json.loads((FIXTURES / "streamer_option.json").read_text())
    row = payload["data"][0]["content"][0]
    quote = decode_option_row(row)
    assert quote.symbol == "AAPL_2026-09-18_C_110.0"
    assert quote.delta == pytest.approx(0.42)
    assert quote.gamma == pytest.approx(0.05)
    assert quote.iv == pytest.approx(0.325)
    assert quote.strike == pytest.approx(110.0)
    assert quote.right == "C"
    assert quote.dte == 120
    assert quote.underlying_price == pytest.approx(183.77)
    assert quote.preferred_price() == pytest.approx(8.45)


def test_merge_partial_change_update():
    base = decode_equity_row(
        {
            "key": "AAPL",
            "delayed": False,
            "1": 100.0,
            "2": 101.0,
            "3": 100.5,
            "33": 100.4,
            "35": 1668715930570,
        }
    )
    updated = merge_quote_update(base, {"key": "AAPL", "3": 101.25, "33": 101.2}, is_option=False)
    assert updated.bid == pytest.approx(100.0)
    assert updated.last == pytest.approx(101.25)
    assert updated.mark == pytest.approx(101.2)


def test_process_streamer_message_merges_into_cache():
    payload = json.loads((FIXTURES / "streamer_equity.json").read_text())
    cache: dict = {}
    process_streamer_message(payload, cache)
    assert "AAPL" in cache
    assert cache["AAPL"].mark == pytest.approx(183.77)


def test_reconnect_triggered_on_login_denied():
    cache: dict = {}
    triggered = []

    process_streamer_message(
        {
            "response": [
                {
                    "service": "ADMIN",
                    "command": "LOGIN",
                    "content": {"code": 3, "msg": "Login Denied"},
                }
            ]
        },
        cache,
        on_reconnect=lambda: triggered.append(True),
    )
    assert triggered == [True]


class _FakeWebSocket:
    def __init__(self, inbound: list[str]):
        self._inbound = list(inbound)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._inbound:
            await asyncio.sleep(3600)
        return self._inbound.pop(0)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def test_streamer_client_login_and_subscribe():
    login_ok = json.dumps(
        {
            "response": [
                {
                    "service": "ADMIN",
                    "command": "LOGIN",
                    "content": {"code": 0, "msg": "server=test;status=PN"},
                }
            ]
        }
    )
    equity_data = (FIXTURES / "streamer_equity.json").read_text()
    ws = _FakeWebSocket([login_ok])

    client = StreamerClient(
        streamer_info=STREAMER_INFO,
        ws_connect=lambda _url: ws,
        access_token_fn=lambda: "test-token",
    )
    client._equity_symbols = {"AAPL"}

    async def run() -> None:
        await client._login(ws, STREAMER_INFO)
        await client._send_subscriptions(ws, STREAMER_INFO)

    asyncio.run(run())
    client._handle_raw_message(equity_data)

    assert any("LOGIN" in msg for msg in ws.sent)
    assert any("LEVELONE_EQUITIES" in msg for msg in ws.sent)
    quote = client.latest("AAPL")
    assert quote is not None
    assert quote.mark == pytest.approx(183.77)
