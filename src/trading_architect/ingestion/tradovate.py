"""Tradovate CSV adapter — fill exports and performance round-trip exports."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from trading_architect.config.defaults import FUTURES_POINT_VALUES
from trading_architect.ingestion.base import BrokerAdapter
from trading_architect.ingestion.parsing import parse_quantity, parse_timestamp, read_broker_csv
from trading_architect.models.entities import (
    AssetType,
    ReviewQueueItem,
    Side,
    Silo,
    TradeEvent,
)


class TradovateAdapter(BrokerAdapter):
    broker_name = "tradovate"

    def parse_csv(
        self, path: Path, account: str | None = None
    ) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
        account = account or "tradovate"
        df = read_broker_csv(path)
        df.columns = [c.strip() for c in df.columns]

        if self._is_performance_export(df):
            return self._parse_performance(df, path.name, account)

        return self._parse_fills(df, path.name, account)

    def _is_performance_export(self, df: pd.DataFrame) -> bool:
        cols = {c.lower() for c in df.columns}
        return {"buyfillid", "sellfillid", "boughttimestamp", "soldtimestamp"}.issubset(cols)

    def _parse_performance(
        self, df: pd.DataFrame, source: str, account: str
    ) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
        """Tradovate performance export — each row is a completed round trip."""
        events: list[TradeEvent] = []
        review: list[ReviewQueueItem] = []

        for idx, row in df.iterrows():
            row_dict = row.to_dict()
            try:
                symbol_raw = str(row["symbol"]).strip().upper()
                qty = parse_quantity(row["qty"])
                buy_price = float(row["buyPrice"])
                sell_price = float(row["sellPrice"])
                buy_ts = parse_timestamp(row["boughtTimestamp"])
                sell_ts = parse_timestamp(row["soldTimestamp"])
                buy_fill_id = str(int(row["buyFillId"]))
                sell_fill_id = str(int(row["sellFillId"]))
                underlying = self._root_symbol(symbol_raw)

                # soldTimestamp before boughtTimestamp => short (sell entry, buy exit)
                if sell_ts <= buy_ts:
                    legs = (
                        (Side.SELL, sell_ts, sell_price, sell_fill_id),
                        (Side.BUY, buy_ts, buy_price, buy_fill_id),
                    )
                else:
                    legs = (
                        (Side.BUY, buy_ts, buy_price, buy_fill_id),
                        (Side.SELL, sell_ts, sell_price, sell_fill_id),
                    )

                for side, ts, price, fill_id in legs:
                    events.append(
                        TradeEvent(
                            broker="tradovate",
                            account=account,
                            timestamp=ts,
                            symbol=symbol_raw,
                            underlying=underlying,
                            asset_type=AssetType.FUTURE,
                            side=side,
                            quantity=qty,
                            price=price,
                            silo=Silo.FUTURES,
                            fill_id=fill_id,
                            raw_ref=f"{source}::fill_{fill_id}",
                        )
                    )
            except Exception as exc:
                review.append(self._review(source, int(idx), str(exc), row_dict))

        return events, review

    def _parse_fills(
        self, df: pd.DataFrame, source: str, account: str
    ) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
        """Standard Tradovate fill/transaction export."""
        events: list[TradeEvent] = []
        review: list[ReviewQueueItem] = []

        date_col = self._find_column(df, ["Timestamp", "Date", "Fill Time", "Trade Date"])
        symbol_col = self._find_column(df, ["Contract", "Symbol", "Product", "symbol"])
        qty_col = self._find_column(df, ["Qty", "Quantity", "Fill Qty", "qty"])
        price_col = self._find_column(df, ["Price", "Fill Price", "Avg Fill Price", "buyPrice"])
        side_col = self._find_column(df, ["B/S", "Side", "Action"])

        fill_id_col = self._find_column(df, ["FillId", "Fill ID", "fillId", "Order ID"])

        if not all([date_col, symbol_col, qty_col, price_col]):
            raise ValueError(
                f"Unrecognized Tradovate CSV format in {source}. "
                f"Columns found: {list(df.columns)}"
            )

        for idx, row in df.iterrows():
            row_dict = row.to_dict()
            try:
                symbol_raw = str(row[symbol_col]).strip()
                if not symbol_raw or symbol_raw.lower() == "nan":
                    continue

                ts = parse_timestamp(row[date_col])
                qty = parse_quantity(row[qty_col])
                price = float(row[price_col])
                if qty == 0:
                    continue

                side_raw = str(row.get(side_col, "Buy")).upper() if side_col else "BUY"
                side = Side.BUY if side_raw in {"B", "BUY", "BOT"} else Side.SELL
                underlying = self._root_symbol(symbol_raw)

                fill_id = None
                if fill_id_col and pd.notna(row.get(fill_id_col)):
                    fill_id = str(int(float(row[fill_id_col])))

                events.append(
                    TradeEvent(
                        broker="tradovate",
                        account=account,
                        timestamp=ts,
                        symbol=symbol_raw.upper(),
                        underlying=underlying,
                        asset_type=AssetType.FUTURE,
                        side=side,
                        quantity=qty,
                        price=price,
                        silo=Silo.FUTURES,
                        fill_id=fill_id,
                        raw_ref=(
                            f"{source}::fill_{fill_id}"
                            if fill_id
                            else f"{source}::row_{idx}"
                        ),
                    )
                )
            except Exception as exc:
                review.append(self._review(source, int(idx), str(exc), row_dict))

        return events, review

    def _find_column(self, df: pd.DataFrame, candidates: list[str]) -> str | None:
        lower_map = {c.lower(): c for c in df.columns}
        for candidate in candidates:
            if candidate.lower() in lower_map:
                return lower_map[candidate.lower()]
        return None

    def _root_symbol(self, contract: str) -> str:
        contract = contract.upper()
        for root in sorted(FUTURES_POINT_VALUES.keys(), key=len, reverse=True):
            if contract.startswith(root):
                return root
        return contract[:3] if len(contract) >= 3 else contract
