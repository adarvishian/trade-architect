"""Robinhood CSV adapter (individual + IRA accounts)."""

from __future__ import annotations

from pathlib import Path

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

TRADE_CODES = {"BTO", "STC", "BUY", "SELL", "OEXP"}
SKIP_CODES = {"CDIV", "ACH", "INT", "GOLD", "MINT", "SLIP", "SPR", "RTP", ""}


class RobinhoodAdapter(BrokerAdapter):
    broker_name = "robinhood"

    def parse_csv(
        self, path: Path, account: str | None = None
    ) -> tuple[list[TradeEvent], list[ReviewQueueItem]]:
        account = account or "robinhood-individual"
        df, parse_review = read_broker_csv(path)
        df.columns = [c.strip() for c in df.columns]

        events: list[TradeEvent] = []
        review: list[ReviewQueueItem] = list(parse_review)
        source = path.name

        for idx, row in df.iterrows():
            row_dict = row.to_dict()
            try:
                trans_code = str(row.get("Trans Code", "")).strip().upper()
                if trans_code in SKIP_CODES:
                    continue
                if trans_code not in TRADE_CODES:
                    continue

                instrument = str(row.get("Instrument", "")).strip()
                description = str(row.get("Description", "")).strip()
                if not instrument or instrument.lower() == "nan":
                    if trans_code != "OEXP":
                        continue
                if "informational purposes only" in description.lower():
                    continue

                ts = parse_timestamp(row.get("Activity Date") or row.get("Process Date"))
                qty = parse_quantity(row.get("Quantity", 0))
                if qty == 0 and trans_code != "OEXP":
                    continue

                # Options: contract details usually in Description; Instrument holds ticker only
                if trans_code in {"BTO", "STC", "OEXP"}:
                    asset_type, symbol, underlying, option_spec = parse_option_from_text(
                        description, underlying_fallback=instrument
                    )
                    if asset_type.value == "stock":
                        asset_type, symbol, underlying, option_spec = parse_option_from_text(
                            instrument, underlying_fallback=instrument
                        )
                else:
                    asset_type, symbol, underlying, option_spec = parse_option_from_text(
                        instrument, underlying_fallback=instrument
                    )

                if trans_code == "OEXP":
                    # Expired worthless — record as sell-to-close at zero
                    side = Side.SELL
                    price = 0.0
                    if qty == 0:
                        qty = parse_quantity(str(row.get("Quantity", "1")))
                elif trans_code in {"BTO", "BUY"}:
                    side = Side.BUY
                    price = parse_money(row.get("Price"))
                else:
                    side = Side.SELL
                    price = parse_money(row.get("Price"))

                events.append(
                    TradeEvent(
                        broker="robinhood",
                        account=account,
                        timestamp=ts,
                        symbol=symbol,
                        underlying=underlying,
                        asset_type=asset_type,
                        side=side,
                        quantity=qty,
                        price=price,
                        fees=0.0,
                        option_spec=option_spec,
                        silo=Silo.STOCK_OPTIONS,
                        raw_ref=f"{source}::row_{idx}",
                    )
                )
            except Exception as exc:
                review.append(self._review(source, int(idx), str(exc), row_dict))

        return events, review
