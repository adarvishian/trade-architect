"""Canonical data entities per PRD §6.1."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Silo(str, Enum):
    STOCK_OPTIONS = "stock_options"
    FUTURES = "futures"


class AssetType(str, Enum):
    STOCK = "stock"
    OPTION = "option"
    FUTURE = "future"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class OptionSpec(BaseModel):
    right: str  # C or P
    strike: float
    expiry: date
    dte_at_entry: int | None = None
    delta_at_entry: float | None = None
    iv_at_entry: float | None = None


class TradeEvent(BaseModel):
    """Atomic fill — canonical schema from broker adapters."""

    event_id: str | None = None
    broker: str
    account: str
    timestamp: datetime
    symbol: str
    underlying: str
    asset_type: AssetType
    side: Side
    quantity: float
    price: float
    fees: float = 0.0
    option_spec: OptionSpec | None = None
    stop_price: float | None = None
    silo: Silo
    raw_ref: str
    epoch_id: str | None = None
    fill_id: str | None = None

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @property
    def content_fingerprint(self) -> str:
        """Fill body identity for cross-key dedup (ignores fill_id / raw_ref)."""
        from trading_architect.ingestion.dedup import content_fingerprint

        return content_fingerprint(self)

    @property
    def natural_key(self) -> str:
        """Stable dedup key — broker fill id when available, else content fingerprint."""
        from trading_architect.ingestion.dedup import natural_key_for

        return natural_key_for(self)

    def signed_quantity(self) -> float:
        return self.quantity if self.side == Side.BUY else -self.quantity


class MethodologyEpoch(BaseModel):
    epoch_id: str
    start_date: date
    end_date: date | None = None
    label: str
    notes: str = ""


class Position(BaseModel):
    position_id: str | None = None
    silo: Silo
    underlying: str
    direction: Direction
    status: PositionStatus
    component_event_ids: list[str] = Field(default_factory=list)
    blended_cost_basis: dict[str, float] = Field(default_factory=dict)
    leg_net_qty: dict[str, float] = Field(default_factory=dict)
    account_leg_qty: dict[str, dict[str, float]] = Field(default_factory=dict)
    current_delta_notional: float = 0.0
    premium_at_risk: float = 0.0
    stop_risk: float = 0.0
    total_dollar_risk: float = 0.0
    epoch_id: str | None = None
    realized_r: float | None = None
    open_r: float | None = None
    realized_pnl: float = 0.0
    opened_at: datetime | None = None
    closed_at: datetime | None = None


class ChainContract(BaseModel):
    strike: float
    expiry: date
    dte: int
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    open_interest: int | None = None
    right: str = "C"


class ChainSnapshot(BaseModel):
    snapshot_id: str | None = None
    underlying: str
    asof_timestamp: datetime
    spot_price: float
    risk_free_rate: float = 0.05
    contracts: list[ChainContract] = Field(default_factory=list)
    iv_rank: float | None = None
    iv_percentile: float | None = None


class ReviewQueueItem(BaseModel):
    item_id: str | None = None
    source_file: str
    row_index: int
    reason: str
    raw_row: dict[str, Any]
    created_at: datetime | None = None


class ImportResult(BaseModel):
    imported: int = 0
    skipped_duplicates: int = 0
    review_queue: int = 0
    source_file: str = ""


class BrokerAccountBalance(BaseModel):
    """Cash and equity for one brokerage account label."""

    account: str
    account_type: str = ""
    cash: float = 0.0
    cash_equivalents: float = 0.0
    cash_and_equivalents: float = 0.0
    portfolio_equity: float = 0.0
    buying_power: float = 0.0


class HoldingLeg(BaseModel):
    """Single open leg in one account (stock shares or option contracts)."""

    account: str
    symbol: str
    underlying: str
    asset_type: AssetType
    quantity: float
    average_cost: float = 0.0
    market_value: float = 0.0


class ConsolidatedHolding(BaseModel):
    """Same symbol/underlying rolled up across accounts with sum-of-parts."""

    symbol: str
    underlying: str
    asset_type: AssetType
    total_quantity: float
    by_account: dict[str, float] = Field(default_factory=dict)
    average_cost: float = 0.0
    market_value: float = 0.0


class RobinhoodPortfolioSnapshot(BaseModel):
    """Live Robinhood book: balances per account and consolidated holdings."""

    fetched_at: datetime
    accounts: list[BrokerAccountBalance] = Field(default_factory=list)
    holdings: list[ConsolidatedHolding] = Field(default_factory=list)

    @property
    def total_cash_and_equivalents(self) -> float:
        return sum(a.cash_and_equivalents for a in self.accounts)

    @property
    def total_portfolio_equity(self) -> float:
        return sum(a.portfolio_equity for a in self.accounts)


class SchwabAccountSnapshot(BaseModel):
    """Live Schwab/thinkorswim book: balances per account and consolidated holdings."""

    fetched_at: datetime
    accounts: list[BrokerAccountBalance] = Field(default_factory=list)
    holdings: list[ConsolidatedHolding] = Field(default_factory=list)

    @property
    def total_cash_and_equivalents(self) -> float:
        return sum(a.cash_and_equivalents for a in self.accounts)

    @property
    def total_portfolio_equity(self) -> float:
        return sum(a.portfolio_equity for a in self.accounts)
