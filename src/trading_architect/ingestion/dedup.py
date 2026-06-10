"""Trade event deduplication — natural keys, content fingerprints, and batch logic."""

from __future__ import annotations

from datetime import timezone

from trading_architect.models.entities import TradeEvent


def normalize_quantity(value: float) -> str:
    """Stable string for fingerprinting — strips float noise."""
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def normalize_price(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def content_fingerprint(event: TradeEvent) -> str:
    """Fill body identity — broker + account + execution details (no fill_id)."""
    ts = event.timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return (
        f"{event.broker}|{event.account}|{ts}|{event.symbol.upper()}|"
        f"{event.side.value}|{normalize_quantity(event.quantity)}|"
        f"{normalize_price(event.price)}"
    )


def natural_key_for(event: TradeEvent) -> str:
    """Stable dedup key — broker fill id when available, else content fingerprint."""
    if event.fill_id:
        return f"{event.broker}|{event.account}|{event.fill_id}"
    return content_fingerprint(event)


def prefer_event(existing: TradeEvent, candidate: TradeEvent) -> TradeEvent:
    """Pick the canonical row when two events represent the same fill."""
    if existing.fill_id and not candidate.fill_id:
        return existing
    if candidate.fill_id and not existing.fill_id:
        return candidate
    return existing


def dedupe_incoming_batch(events: list[TradeEvent]) -> tuple[list[TradeEvent], int]:
    """Remove duplicates within a single import batch; prefer fill_id rows."""
    if not events:
        return [], 0

    by_fingerprint: dict[str, TradeEvent] = {}
    by_natural_key: dict[str, TradeEvent] = {}
    skipped = 0

    for event in events:
        fp = content_fingerprint(event)
        nk = natural_key_for(event)

        if nk in by_natural_key:
            skipped += 1
            continue

        existing = by_fingerprint.get(fp)
        if existing is not None:
            kept = prefer_event(existing, event)
            if kept is existing:
                skipped += 1
                continue
            old_nk = natural_key_for(existing)
            by_natural_key.pop(old_nk, None)
            skipped += 1

        by_fingerprint[fp] = event
        by_natural_key[nk] = event

    return list(by_fingerprint.values()), skipped
