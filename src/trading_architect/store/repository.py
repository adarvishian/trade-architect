"""Repository layer for normalized store CRUD."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

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
