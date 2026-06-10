"""Tests for MarksProvider implementations."""

from datetime import datetime

from trading_architect.engines.marks import Mark, NullMarksProvider, StaticMarksProvider


def test_null_marks_provider_returns_empty():
    assert NullMarksProvider().marks_for(["AAPL"]) == {}


def test_static_marks_provider():
    mark = Mark(symbol="AAPL", price=195.0, delta=None, asof=datetime(2026, 5, 21))
    provider = StaticMarksProvider({"AAPL": mark})
    result = provider.marks_for(["AAPL", "MSFT"])
    assert len(result) == 1
    assert result["AAPL"].price == 195.0


class _FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._data


class _FakeClient:
    def get_quotes(self, symbols):
        out = {}
        for s in symbols:
            out[s] = {"quote": {"markPrice": 100.0, "delta": 0.5 if "C" in s else None}}
        return _FakeResponse(out)


def test_schwab_marks_provider_caches():
    from trading_architect.engines.marks import SchwabMarksProvider

    provider = SchwabMarksProvider(client=_FakeClient(), ttl_seconds=3600, use_streamer=False)
    m1 = provider.marks_for(["AAPL"])
    m2 = provider.marks_for(["AAPL"])
    assert m1["AAPL"].price == 100.0
    assert m2["AAPL"].price == 100.0
