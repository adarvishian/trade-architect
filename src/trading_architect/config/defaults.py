"""Default configuration — all user-overridable per PRD §7.3 / §7.4."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from trading_architect.models.entities import MethodologyEpoch

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
RAW_CSV_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "trading_architect.db"

# Methodology change ~late March 2026 (PRD §3, §6.1)
METHODOLOGY_CHANGE_DATE = date(2026, 3, 27)

DEFAULT_EPOCHS: list[MethodologyEpoch] = [
    MethodologyEpoch(
        epoch_id="pre-2026-03-27",
        start_date=date(2000, 1, 1),
        end_date=date(2026, 3, 26),
        label="Pre-methodology change",
        notes="Sizing calibrated to smaller account; pre late-March 2026 methodology.",
    ),
    MethodologyEpoch(
        epoch_id="post-2026-03-27",
        start_date=date(2026, 3, 27),
        end_date=None,
        label="Post-methodology change",
        notes="Revised methodology expected to improve performance.",
    ),
]

# Standard U.S. equity option contract size (100 shares per contract)
OPTION_CONTRACT_MULTIPLIER = 100

# Sizing defaults (Phase 1 synthesis + PRD Appendix A)
DEFAULT_BASE_RISK_F = 0.01  # 1% of silo equity per trade
DEFAULT_KELLY_FRACTION = 0.25
DEFAULT_HEAT_CAP = 0.10  # 10% portfolio heat
DEFAULT_LEVERAGE_CAP = 2.0  # delta-notional multiple of silo equity

# Drawdown governor (PRD §7.7)
DRAWDOWN_SOFT_ALERT = 0.10
DRAWDOWN_THROTTLE_START = 0.15
DRAWDOWN_HARD_CAP = 0.20

# Opportunity correlation / attribution-aware governor (PRD §7.3 / §7.7)
DEFAULT_CORRELATION_THRESHOLD = 0.5  # pair at/above this is treated as one effective opportunity
DEFAULT_ASSUMED_CORRELATION = 0.6  # assumed corr between distinct names when no return data

# Futures point values (extend as needed)
FUTURES_POINT_VALUES: dict[str, float] = {
    "ES": 50.0,
    "MES": 5.0,
    "NQ": 20.0,
    "MNQ": 2.0,
    "CL": 1000.0,
    "MCL": 100.0,
    "GC": 100.0,
    "MGC": 10.0,
}
