"""Charles Schwab / thinkorswim transaction CSV adapter."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from trading_architect.ingestion.base import BrokerAdapter
from trading_architect.ingestion.parsing import (
    parse_money,
    parse_option_from_text,
    parse_quantity,
    parse_timestamp,
    read_broker_csv,
)
from trading_architect.models.entities import (
    ReviewQueueItem,
    Side,
    Silo,
    TradeEvent,
)

TRADE_ACTIONS = {
    "BUY",
    "SELL",
    "BUY TO OPEN",
    "SELL TO CLOSE",
    "BUY TO CLOSE",
    "SELL TO OPEN",
    "EXPIRED",
}


class SchwabAdapter(BrokerAdapter):
    broker_name = "schwab"

    def parse_csv(
        self, path: Path, account: str | None = None
    ) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
        account = account or "schwab-tos"
        df, parse_review = read_broker_csv(path)
        df.columns = [c.strip() for c in df.columns]

        events: list[TradeEvent] = []
        review: list[ReviewQueueItem] = list(parse_review)
        source = path.name

        for idx, row in df.iterrows():
            row_dict = row.to_dict()
            try:
                action = str(row.get("Action", "")).strip().upper()
                if not action or action not in TRADE_ACTIONS:
                    continue

                symbol_raw = str(row.get("Symbol", "")).strip()
                if not symbol_raw or symbol_raw.lower() == "nan":
                    continue

                ts = parse_timestamp(row.get("Date"))
                qty = parse_quantity(row.get("Quantity", row.get("Qty", 0)))
                if qty == 0 and action != "EXPIRED":
                    continue

                fees_raw = row.get("Fees & Comm", 0)
                fees = (
                    parse_money(fees_raw) if pd.notna(fees_raw) and str(fees_raw).strip() else 0.0
                )
                fees = abs(fees)

                asset_type, symbol, underlying, option_spec = parse_option_from_text(symbol_raw)

                if action in {"BUY", "BUY TO OPEN", "BUY TO CLOSE"}:
                    side = Side.BUY
                elif action == "EXPIRED":
                    side = Side.SELL
                else:
                    side = Side.SELL

                if action == "EXPIRED":
                    price = 0.0
                    qty = abs(qty)
                else:
                    price = parse_money(row.get("Price"))

                events.append(
                    TradeEvent(
                        broker="schwab",
                        account=account,
                        timestamp=ts,
                        symbol=symbol,
                        underlying=underlying,
                        asset_type=asset_type,
                        side=side,
                        quantity=qty,
                        price=price,
                        fees=fees,
                        option_spec=option_spec,
                        silo=Silo.STOCK_OPTIONS,
                        raw_ref=f"{source}::row_{idx}",
                    )
                )
            except Exception as exc:
                review.append(self._review(source, int(idx), str(exc), row_dict))

        return events, review
