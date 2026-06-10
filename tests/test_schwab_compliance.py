"""AC-8: Schwab integration must not contain order or transfer code paths."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "trading_architect"

FORBIDDEN_PATTERNS = [
    re.compile(r"previewOrder", re.IGNORECASE),
    re.compile(r"placeOrder", re.IGNORECASE),
    re.compile(r"/orders\b", re.IGNORECASE),
    re.compile(
        r"""(?:requests?\.(?:post|put|delete)|\.(?:post|put|delete)\s*\()\s*[^)]*['"]/trader""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""['"]https://api\.schwabapi\.com/trader/v1[^'"]*/(?:orders|transfers)""",
        re.IGNORECASE,
    ),
]


def _schwab_sources() -> list[Path]:
    ingestion = SRC / "ingestion"
    paths = sorted(ingestion.glob("schwab_*.py"))
    marks = SRC / "engines" / "marks.py"
    if marks.is_file():
        paths.append(marks)
    return paths


@pytest.mark.parametrize("path", _schwab_sources(), ids=lambda p: p.name)
def test_no_order_or_transfer_code(path: Path):
    text = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_PATTERNS:
        match = pattern.search(text)
        assert match is None, (
            f"{path.relative_to(ROOT)}: forbidden pattern {pattern.pattern!r} at: {match.group()!r}"
            if match
            else ""
        )
