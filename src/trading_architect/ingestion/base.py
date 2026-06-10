"""Broker CSV ingestion framework."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import pandas as pd

from trading_architect.config.defaults import DEFAULT_EPOCHS
from trading_architect.models.entities import (
    ImportResult,
    ReviewQueueItem,
    TradeEvent,
)


def resolve_epoch_id(trade_date: date) -> str:
    for epoch in DEFAULT_EPOCHS:
        end = epoch.end_date or date(9999, 12, 31)
        if epoch.start_date <= trade_date <= end:
            return epoch.epoch_id
    return DEFAULT_EPOCHS[-1].epoch_id


class BrokerAdapter(ABC):
    broker_name: str

    @abstractmethod
    def parse_csv(
        self, path: Path, account: str | None = None
    ) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
        """Parse broker CSV into canonical TradeEvents and review-queue items."""

    def _review(
        self,
        source_file: str,
        row_index: int,
        reason: str,
        row: dict,
    ) -> ReviewQueueItem:
        return ReviewQueueItem(
            source_file=source_file,
            row_index=row_index,
            reason=reason,
            raw_row={k: (None if pd.isna(v) else v) for k, v in row.items()},
        )


class IngestionService:
    def __init__(self, repo) -> None:
        self.repo = repo
        self._adapters: dict[str, BrokerAdapter] = {}

    def register(self, adapter: BrokerAdapter) -> None:
        self._adapters[adapter.broker_name.lower()] = adapter

    def import_csv(
        self,
        path: Path,
        broker: str,
        account: str | None = None,
        archive: bool = True,
    ) -> ImportResult:
        adapter = self._adapters.get(broker.lower())
        if adapter is None:
            raise ValueError(f"No adapter registered for broker: {broker}")

        if archive:
            self.repo.archive_raw_csv(path)

        events, review_items = adapter.parse_csv(path, account=account)

        for event in events:
            event.epoch_id = resolve_epoch_id(event.timestamp.date())

        review_count = self.repo.add_review_items(review_items)
        result = self.repo.upsert_events(events)
        result.review_queue = review_count
        return result
