"""Repository layer for normalized store CRUD."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from trading_architect.config.user_settings import (
    AppSettings,
    OverrideLogEntry,
    app_settings_from_json,
    app_settings_to_json,
)
from trading_architect.ingestion.dedup import (
    content_fingerprint,
    dedupe_incoming_batch,
    natural_key_for,
    prefer_event,
)
from trading_architect.models.entities import (
    ChainSnapshot,
    ImportResult,
    MethodologyEpoch,
    Position,
    ReviewQueueItem,
    Silo,
    TradeEvent,
)
from trading_architect.store.database import (
    Database,
    event_to_row,
    row_to_chain_snapshot,
    row_to_epoch,
    row_to_event,
    row_to_position,
    row_to_review_item,
)

AccountKind = Literal["schwab", "robinhood", "tradovate", "manual"]
SnapshotSource = Literal["api", "manual"]


@dataclass(frozen=True)
class AccountRecord:
    id: int
    kind: AccountKind
    label: str
    silo: Silo
    institution: str
    include_in_deployable: bool
    created_at: datetime | None = None


@dataclass(frozen=True)
class BalanceSnapshotRecord:
    account_id: int
    as_of: datetime
    cash: float
    equity_value: float
    buying_power: float
    source: SnapshotSource
    id: int | None = None


@dataclass(frozen=True)
class HoldingSnapshotRecord:
    account_id: int
    as_of: datetime
    symbol: str
    asset_type: str
    qty: float
    mtm_value: float
    cost_basis: float
    occ_symbol: str | None = None
    mark: float | None = None
    id: int | None = None


@dataclass(frozen=True)
class PositionOverrideRecord:
    symbol: str
    silo: Silo
    initial_stop: float | None = None
    current_stop: float | None = None
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True)
class SizeRecommendationRecord:
    ts: datetime
    silo: Silo
    underlying: str
    asset_type: str
    recommended_qty: float
    dollar_risk: float
    binding_constraint: str
    inputs_json: str
    silo_equity: float
    id: int | None = None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_to_account(row) -> AccountRecord:
    return AccountRecord(
        id=int(row["id"]),
        kind=row["kind"],
        label=row["label"],
        silo=Silo(row["silo"]),
        institution=row["institution"] or "",
        include_in_deployable=bool(row["include_in_deployable"]),
        created_at=_parse_dt(row["created_at"]),
    )


class Repository:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()
        self.db.initialize()

    def _load_dedup_index(self, conn) -> tuple[set[str], dict[str, tuple[str, TradeEvent]]]:
        """Return (natural_keys, fingerprint -> (event_id, event))."""
        rows = conn.execute(
            "SELECT event_id, natural_key, content_fingerprint, payload_json FROM trade_events"
        ).fetchall()
        natural_keys: set[str] = set()
        by_fingerprint: dict[str, tuple[str, TradeEvent]] = {}
        for row in rows:
            natural_keys.add(row["natural_key"])
            event = row_to_event(row)
            fp = row["content_fingerprint"] or event.content_fingerprint
            existing = by_fingerprint.get(fp)
            if existing is None:
                by_fingerprint[fp] = (row["event_id"], event)
            else:
                kept = prefer_event(existing[1], event)
                if kept is not event:
                    continue
                by_fingerprint[fp] = (row["event_id"], event)
        return natural_keys, by_fingerprint

    def upsert_events(self, events: list[TradeEvent]) -> ImportResult:
        events, batch_skipped = dedupe_incoming_batch(events)
        imported = 0
        skipped = batch_skipped
        source = events[0].raw_ref.split("::")[0] if events else ""

        with self.db.connect() as conn:
            natural_keys, by_fingerprint = self._load_dedup_index(conn)

            for event in events:
                if not event.event_id:
                    event.event_id = str(uuid.uuid4())

                nk = natural_key_for(event)
                fp = content_fingerprint(event)

                if nk in natural_keys:
                    skipped += 1
                    continue

                existing = by_fingerprint.get(fp)
                if existing is not None:
                    existing_id, existing_event = existing
                    if event.fill_id and not existing_event.fill_id:
                        if not event.event_id or event.event_id != existing_id:
                            event.event_id = existing_id
                        conn.execute(
                            """
                            UPDATE trade_events SET
                                event_id = ?, natural_key = ?, content_fingerprint = ?,
                                broker = ?, account = ?, timestamp = ?,
                                symbol = ?, underlying = ?, asset_type = ?, side = ?,
                                quantity = ?, price = ?, fees = ?, option_spec_json = ?,
                                stop_price = ?, silo = ?, raw_ref = ?, epoch_id = ?,
                                payload_json = ?
                            WHERE event_id = ?
                            """,
                            (*event_to_row(event), existing_id),
                        )
                        natural_keys.discard(natural_key_for(existing_event))
                        natural_keys.add(nk)
                        by_fingerprint[fp] = (existing_id, event)
                        continue
                    skipped += 1
                    continue

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO trade_events (
                        event_id, natural_key, content_fingerprint, broker, account,
                        timestamp, symbol, underlying, asset_type, side, quantity, price,
                        fees, option_spec_json, stop_price, silo, raw_ref,
                        epoch_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    event_to_row(event),
                )
                if cursor.rowcount:
                    imported += 1
                    natural_keys.add(nk)
                    by_fingerprint[fp] = (event.event_id, event)
                else:
                    skipped += 1

            conn.execute(
                """
                INSERT INTO import_log (source_file, imported, skipped_duplicates, review_queue_count)
                VALUES (?, ?, ?, 0)
                """,
                (source, imported, skipped),
            )

        return ImportResult(
            imported=imported,
            skipped_duplicates=skipped,
            source_file=source,
        )

    def add_review_items(self, items: list[ReviewQueueItem]) -> int:
        if not items:
            return 0

        with self.db.connect() as conn:
            for item in items:
                conn.execute(
                    """
                    INSERT INTO review_queue (source_file, row_index, reason, raw_row_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        item.source_file,
                        item.row_index,
                        item.reason,
                        json.dumps(item.raw_row),
                    ),
                )
        return len(items)

    def list_events(
        self,
        silo: str | None = None,
        underlying: str | None = None,
    ) -> list[TradeEvent]:
        query = "SELECT payload_json FROM trade_events WHERE 1=1"
        params: list = []
        if silo:
            query += " AND silo = ?"
            params.append(silo)
        if underlying:
            query += " AND underlying = ?"
            params.append(underlying)
        query += " ORDER BY timestamp"

        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_event(r) for r in rows]

    def list_epochs(self) -> list[MethodologyEpoch]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM methodology_epochs ORDER BY start_date").fetchall()
        return [row_to_epoch(r) for r in rows]

    def replace_positions(self, positions: list[Position]) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM positions")
            for pos in positions:
                if not pos.position_id:
                    pos.position_id = str(uuid.uuid4())
                payload = pos.model_dump(mode="json")
                conn.execute(
                    """
                    INSERT INTO positions (
                        position_id, silo, underlying, direction, status,
                        component_event_ids_json, blended_cost_basis_json,
                        current_delta_notional, premium_at_risk, stop_risk,
                        total_dollar_risk, epoch_id, realized_r, open_r,
                        realized_pnl, opened_at, closed_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pos.position_id,
                        pos.silo.value,
                        pos.underlying,
                        pos.direction.value,
                        pos.status.value,
                        json.dumps(pos.component_event_ids),
                        json.dumps(pos.blended_cost_basis),
                        pos.current_delta_notional,
                        pos.premium_at_risk,
                        pos.stop_risk,
                        pos.total_dollar_risk,
                        pos.epoch_id,
                        pos.realized_r,
                        pos.open_r,
                        pos.realized_pnl,
                        pos.opened_at.isoformat() if pos.opened_at else None,
                        pos.closed_at.isoformat() if pos.closed_at else None,
                        json.dumps(payload),
                    ),
                )

    def list_positions(
        self,
        silo: str | None = None,
        status: str | None = None,
    ) -> list[Position]:
        query = "SELECT payload_json FROM positions WHERE 1=1"
        params: list = []
        if silo:
            query += " AND silo = ?"
            params.append(silo)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY opened_at"

        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_position(r) for r in rows]

    def list_review_queue(self) -> list[ReviewQueueItem]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM review_queue ORDER BY created_at DESC").fetchall()
        return [row_to_review_item(r) for r in rows]

    def event_count(self) -> int:
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM trade_events").fetchone()
        return int(row["c"])

    def position_count(self) -> int:
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM positions").fetchone()
        return int(row["c"])

    def save_chain_snapshot(self, snapshot: ChainSnapshot) -> ChainSnapshot:
        if not snapshot.snapshot_id:
            snapshot.snapshot_id = str(uuid.uuid4())
        payload = snapshot.model_dump(mode="json")
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO chain_snapshots (
                    snapshot_id, underlying, asof_timestamp, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.underlying,
                    snapshot.asof_timestamp.isoformat(),
                    json.dumps(payload),
                ),
            )
        return snapshot

    def get_chain_snapshot(self, snapshot_id: str) -> ChainSnapshot | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM chain_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if not row:
            return None
        return row_to_chain_snapshot(row)

    def list_chain_snapshots(self, underlying: str | None = None) -> list[ChainSnapshot]:
        query = "SELECT payload_json FROM chain_snapshots WHERE 1=1"
        params: list = []
        if underlying:
            query += " AND underlying = ?"
            params.append(underlying.upper())
        query += " ORDER BY asof_timestamp DESC"
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row_to_chain_snapshot(r) for r in rows]

    def archive_raw_csv(self, source: Path) -> Path:
        dest_dir = Path(self.db.path).parent / "raw"
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"{ts}_{source.name}"
        dest.write_bytes(source.read_bytes())
        return dest

    APP_SETTINGS_KEY = "app_settings"

    def load_app_settings(self) -> AppSettings:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM user_settings WHERE key = ?",
                (self.APP_SETTINGS_KEY,),
            ).fetchone()
        if not row:
            return AppSettings()
        return app_settings_from_json(row["value_json"])

    def save_app_settings(
        self,
        settings: AppSettings,
        *,
        overrides: list[OverrideLogEntry] | None = None,
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (self.APP_SETTINGS_KEY, app_settings_to_json(settings), datetime.now().isoformat()),
            )
            if overrides:
                for entry in overrides:
                    conn.execute(
                        """
                        INSERT INTO override_log (parameter, old_value, new_value, source, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            entry.parameter,
                            entry.old_value,
                            entry.new_value,
                            entry.source,
                            entry.timestamp.isoformat(),
                        ),
                    )

    def list_override_log(self, limit: int = 50) -> list[OverrideLogEntry]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT parameter, old_value, new_value, source, created_at
                FROM override_log
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            OverrideLogEntry(
                timestamp=datetime.fromisoformat(row["created_at"]),
                parameter=row["parameter"],
                old_value=row["old_value"],
                new_value=row["new_value"],
                source=row["source"],
            )
            for row in rows
        ]

    def list_import_log(self, limit: int = 20) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT source_file, imported, skipped_duplicates, review_queue_count, imported_at
                FROM import_log
                ORDER BY imported_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_content_duplicate_events(self) -> int:
        """Drop duplicate fills (legacy rows, tz-naive/aware pairs, fill-id vs fingerprint)."""
        events = self.list_events()
        best_by_fingerprint: dict[str, TradeEvent] = {}
        to_delete: set[str] = set()

        for event in events:
            fp = content_fingerprint(event)
            existing = best_by_fingerprint.get(fp)
            if existing is None:
                best_by_fingerprint[fp] = event
                continue
            keep = prefer_event(existing, event)
            drop = event if keep is existing else existing
            best_by_fingerprint[fp] = keep
            if drop.event_id:
                to_delete.add(drop.event_id)

        if not to_delete:
            return 0

        with self.db.connect() as conn:
            conn.executemany(
                "DELETE FROM trade_events WHERE event_id = ?",
                [(eid,) for eid in to_delete],
            )
        return len(to_delete)

    def remove_events_with_invalid_underlying(self) -> int:
        """Drop trade events that cannot map to a real ticker (e.g. RH cash-sweep orders)."""
        from trading_architect.ingestion.parsing import is_assemblable_event

        events = self.list_events()
        to_delete = [e.event_id for e in events if e.event_id and not is_assemblable_event(e)]
        if not to_delete:
            return 0

        with self.db.connect() as conn:
            conn.executemany(
                "DELETE FROM trade_events WHERE event_id = ?",
                [(eid,) for eid in to_delete],
            )
        return len(to_delete)

    def upsert_account(
        self,
        *,
        kind: AccountKind,
        label: str,
        silo: Silo,
        institution: str = "",
        include_in_deployable: bool = True,
    ) -> AccountRecord:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts (kind, label, silo, institution, include_in_deployable)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(label) DO UPDATE SET
                    kind = excluded.kind,
                    silo = excluded.silo,
                    institution = excluded.institution,
                    include_in_deployable = excluded.include_in_deployable
                """,
                (kind, label, silo.value, institution, int(include_in_deployable)),
            )
            row = conn.execute("SELECT * FROM accounts WHERE label = ?", (label,)).fetchone()
        return _row_to_account(row)

    def list_accounts(self, *, silo: Silo | None = None) -> list[AccountRecord]:
        query = "SELECT * FROM accounts WHERE 1=1"
        params: list = []
        if silo:
            query += " AND silo = ?"
            params.append(silo.value)
        query += " ORDER BY label"
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_account(r) for r in rows]

    def get_account_by_label(self, label: str) -> AccountRecord | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE label = ?", (label,)).fetchone()
        return _row_to_account(row) if row else None

    def delete_account(self, account_id: int) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM holdings_snapshots WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM balance_snapshots WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def record_balance_snapshot(self, snapshot: BalanceSnapshotRecord) -> BalanceSnapshotRecord:
        as_of = snapshot.as_of.astimezone(timezone.utc).isoformat()
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO balance_snapshots (
                    account_id, as_of, cash, equity_value, buying_power, source
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.account_id,
                    as_of,
                    snapshot.cash,
                    snapshot.equity_value,
                    snapshot.buying_power,
                    snapshot.source,
                ),
            )
            row_id = cursor.lastrowid
        return BalanceSnapshotRecord(
            id=row_id,
            account_id=snapshot.account_id,
            as_of=snapshot.as_of,
            cash=snapshot.cash,
            equity_value=snapshot.equity_value,
            buying_power=snapshot.buying_power,
            source=snapshot.source,
        )

    def record_holdings_snapshots(
        self,
        account_id: int,
        as_of: datetime,
        rows: list[HoldingSnapshotRecord],
    ) -> int:
        as_of_iso = as_of.astimezone(timezone.utc).isoformat()
        if not rows:
            return 0
        with self.db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO holdings_snapshots (
                    account_id, as_of, symbol, occ_symbol, asset_type,
                    qty, mark, mtm_value, cost_basis
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        account_id,
                        as_of_iso,
                        row.symbol,
                        row.occ_symbol,
                        row.asset_type,
                        row.qty,
                        row.mark,
                        row.mtm_value,
                        row.cost_basis,
                    )
                    for row in rows
                ],
            )
        return len(rows)

    def latest_balance(self, account_id: int) -> BalanceSnapshotRecord | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM balance_snapshots
                WHERE account_id = ?
                ORDER BY as_of DESC, id DESC
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        if not row:
            return None
        return BalanceSnapshotRecord(
            id=row["id"],
            account_id=row["account_id"],
            as_of=_parse_dt(row["as_of"]),
            cash=row["cash"],
            equity_value=row["equity_value"],
            buying_power=row["buying_power"],
            source=row["source"],
        )

    def latest_balances(self) -> dict[int, BalanceSnapshotRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.* FROM balance_snapshots b
                INNER JOIN (
                    SELECT account_id, MAX(as_of) AS max_as_of
                    FROM balance_snapshots
                    GROUP BY account_id
                ) latest
                ON b.account_id = latest.account_id AND b.as_of = latest.max_as_of
                """
            ).fetchall()
        result: dict[int, BalanceSnapshotRecord] = {}
        for row in rows:
            snap = BalanceSnapshotRecord(
                id=row["id"],
                account_id=row["account_id"],
                as_of=_parse_dt(row["as_of"]),
                cash=row["cash"],
                equity_value=row["equity_value"],
                buying_power=row["buying_power"],
                source=row["source"],
            )
            existing = result.get(snap.account_id)
            if existing is None or (snap.id or 0) > (existing.id or 0):
                result[snap.account_id] = snap
        return result

    def balance_history(self, account_id: int) -> list[BalanceSnapshotRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM balance_snapshots
                WHERE account_id = ?
                ORDER BY as_of ASC, id ASC
                """,
                (account_id,),
            ).fetchall()
        return [
            BalanceSnapshotRecord(
                id=row["id"],
                account_id=row["account_id"],
                as_of=_parse_dt(row["as_of"]),
                cash=row["cash"],
                equity_value=row["equity_value"],
                buying_power=row["buying_power"],
                source=row["source"],
            )
            for row in rows
        ]

    def latest_holdings(
        self,
        account_id: int | None = None,
    ) -> list[HoldingSnapshotRecord]:
        """Latest holdings batch per account (or all accounts when account_id is None)."""
        with self.db.connect() as conn:
            if account_id is not None:
                latest = conn.execute(
                    """
                    SELECT MAX(as_of) AS max_as_of FROM holdings_snapshots
                    WHERE account_id = ?
                    """,
                    (account_id,),
                ).fetchone()
                if not latest or not latest["max_as_of"]:
                    return []
                rows = conn.execute(
                    """
                    SELECT * FROM holdings_snapshots
                    WHERE account_id = ? AND as_of = ?
                    ORDER BY symbol
                    """,
                    (account_id, latest["max_as_of"]),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT h.* FROM holdings_snapshots h
                    INNER JOIN (
                        SELECT account_id, MAX(as_of) AS max_as_of
                        FROM holdings_snapshots
                        GROUP BY account_id
                    ) latest
                    ON h.account_id = latest.account_id AND h.as_of = latest.max_as_of
                    ORDER BY h.account_id, h.symbol
                    """
                ).fetchall()
        return [
            HoldingSnapshotRecord(
                id=row["id"],
                account_id=row["account_id"],
                as_of=_parse_dt(row["as_of"]),
                symbol=row["symbol"],
                occ_symbol=row["occ_symbol"],
                asset_type=row["asset_type"],
                qty=row["qty"],
                mark=row["mark"],
                mtm_value=row["mtm_value"],
                cost_basis=row["cost_basis"],
            )
            for row in rows
        ]

    def update_epoch(self, epoch: MethodologyEpoch) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE methodology_epochs
                SET start_date = ?, end_date = ?, label = ?, notes = ?
                WHERE epoch_id = ?
                """,
                (
                    epoch.start_date.isoformat(),
                    epoch.end_date.isoformat() if epoch.end_date else None,
                    epoch.label,
                    epoch.notes,
                    epoch.epoch_id,
                ),
            )

    def upsert_position_override(
        self,
        *,
        symbol: str,
        silo: Silo,
        initial_stop: float | None,
        current_stop: float | None,
    ) -> PositionOverrideRecord:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO position_overrides (symbol, silo, initial_stop, current_stop, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol, silo) DO UPDATE SET
                    initial_stop = COALESCE(excluded.initial_stop, position_overrides.initial_stop),
                    current_stop = excluded.current_stop,
                    updated_at = excluded.updated_at
                """,
                (symbol, silo.value, initial_stop, current_stop, now),
            )
            row = conn.execute(
                """
                SELECT * FROM position_overrides WHERE symbol = ? AND silo = ?
                """,
                (symbol, silo.value),
            ).fetchone()
        return PositionOverrideRecord(
            id=row["id"],
            symbol=row["symbol"],
            silo=Silo(row["silo"]),
            initial_stop=row["initial_stop"],
            current_stop=row["current_stop"],
            updated_at=_parse_dt(row["updated_at"]),
        )

    def list_position_overrides(self, *, silo: Silo | None = None) -> list[PositionOverrideRecord]:
        query = "SELECT * FROM position_overrides WHERE 1=1"
        params: list = []
        if silo:
            query += " AND silo = ?"
            params.append(silo.value)
        query += " ORDER BY symbol"
        with self.db.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            PositionOverrideRecord(
                id=row["id"],
                symbol=row["symbol"],
                silo=Silo(row["silo"]),
                initial_stop=row["initial_stop"],
                current_stop=row["current_stop"],
                updated_at=_parse_dt(row["updated_at"]),
            )
            for row in rows
        ]

    def get_position_override(self, symbol: str, silo: Silo) -> PositionOverrideRecord | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM position_overrides WHERE symbol = ? AND silo = ?",
                (symbol, silo.value),
            ).fetchone()
        if not row:
            return None
        return PositionOverrideRecord(
            id=row["id"],
            symbol=row["symbol"],
            silo=Silo(row["silo"]),
            initial_stop=row["initial_stop"],
            current_stop=row["current_stop"],
            updated_at=_parse_dt(row["updated_at"]),
        )

    def record_size_recommendation(
        self,
        *,
        ts: datetime,
        silo: Silo,
        underlying: str,
        asset_type: str,
        recommended_qty: float,
        dollar_risk: float,
        binding_constraint: str,
        inputs_json: str,
        silo_equity: float,
    ) -> SizeRecommendationRecord:
        ts_iso = ts.astimezone(timezone.utc).isoformat()
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO size_recommendations (
                    ts, silo, underlying, asset_type, recommended_qty,
                    dollar_risk, binding_constraint, inputs_json, silo_equity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_iso,
                    silo.value,
                    underlying,
                    asset_type,
                    recommended_qty,
                    dollar_risk,
                    binding_constraint,
                    inputs_json,
                    silo_equity,
                ),
            )
            row_id = cursor.lastrowid
        return SizeRecommendationRecord(
            id=row_id,
            ts=ts,
            silo=silo,
            underlying=underlying,
            asset_type=asset_type,
            recommended_qty=recommended_qty,
            dollar_risk=dollar_risk,
            binding_constraint=binding_constraint,
            inputs_json=inputs_json,
            silo_equity=silo_equity,
        )

    def list_size_recommendations(self, limit: int = 50) -> list[SizeRecommendationRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM size_recommendations
                ORDER BY ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            SizeRecommendationRecord(
                id=row["id"],
                ts=_parse_dt(row["ts"]),
                silo=Silo(row["silo"]),
                underlying=row["underlying"],
                asset_type=row["asset_type"],
                recommended_qty=row["recommended_qty"],
                dollar_risk=row["dollar_risk"],
                binding_constraint=row["binding_constraint"],
                inputs_json=row["inputs_json"],
                silo_equity=row["silo_equity"],
            )
            for row in rows
        ]
