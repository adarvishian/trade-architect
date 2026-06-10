"""Tests for trade event deduplication."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_architect.bootstrap import create_repository, import_and_assemble
from trading_architect.ingestion.dedup import (
    content_fingerprint,
    dedupe_incoming_batch,
    natural_key_for,
)
from trading_architect.models.entities import AssetType, Side, Silo, TradeEvent

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("trading_architect.config.defaults.DB_PATH", db_path)
    monkeypatch.setattr("trading_architect.bootstrap.DB_PATH", db_path)
    yield db_path


def _sample_event(**overrides) -> TradeEvent:
    defaults = dict(
        broker="tradovate",
        account="tradovate",
        timestamp=datetime(2026, 5, 11, 10, 45, 16, tzinfo=timezone.utc),
        symbol="MCLM6",
        underlying="MCL",
        asset_type=AssetType.FUTURE,
        side=Side.BUY,
        quantity=1,
        price=98.23,
        silo=Silo.FUTURES,
        raw_ref="test::row_0",
    )
    defaults.update(overrides)
    return TradeEvent(**defaults)


def test_content_fingerprint_normalizes_float_noise():
    a = _sample_event(price=98.2300000001, quantity=1.0000001)
    b = _sample_event(price=98.23, quantity=1)
    assert content_fingerprint(a) == content_fingerprint(b)


def test_content_fingerprint_normalizes_timezone():
    aware = _sample_event(timestamp=datetime(2026, 5, 11, 10, 45, 16, tzinfo=timezone.utc))
    naive = _sample_event(timestamp=datetime(2026, 5, 11, 10, 45, 16))
    assert content_fingerprint(aware) == content_fingerprint(naive)


def test_natural_key_uses_fill_id_when_present():
    with_fill = _sample_event(fill_id="11718233245")
    assert natural_key_for(with_fill) == "tradovate|tradovate|11718233245"
    assert natural_key_for(_sample_event()) == content_fingerprint(_sample_event())


def test_dedupe_incoming_batch_prefers_fill_id():
    legacy = _sample_event(raw_ref="legacy::0")
    keyed = _sample_event(fill_id="11718233245", raw_ref="new::0")
    deduped, skipped = dedupe_incoming_batch([legacy, keyed])
    assert len(deduped) == 1
    assert skipped == 1
    assert deduped[0].fill_id == "11718233245"


def test_upsert_skips_existing_natural_key(temp_db):
    repo = create_repository()
    event = _sample_event()
    r1 = repo.upsert_events([event])
    r2 = repo.upsert_events([_sample_event(raw_ref="other::row")])
    assert r1.imported == 1
    assert r2.imported == 0
    assert r2.skipped_duplicates == 1
    assert repo.event_count() == 1


def test_upsert_upgrades_legacy_when_fill_id_reimported(temp_db):
    """fill_id reimport upgrades an existing fingerprint-only row instead of duplicating."""
    repo = create_repository()
    legacy = _sample_event(raw_ref="legacy::0")
    repo.upsert_events([legacy])

    keyed = _sample_event(fill_id="11718233245", raw_ref="keyed::0")
    result = repo.upsert_events([keyed])
    assert result.imported == 0
    assert result.skipped_duplicates == 0
    assert repo.event_count() == 1
    assert repo.list_events()[0].fill_id == "11718233245"


def test_upsert_skips_legacy_when_fill_id_row_exists(temp_db):
    """Fingerprint-only row should not duplicate an existing fill_id row."""
    repo = create_repository()
    keyed = _sample_event(fill_id="11718233245", raw_ref="keyed::0")
    repo.upsert_events([keyed])

    legacy = _sample_event(raw_ref="legacy::0")
    result = repo.upsert_events([legacy])
    assert result.imported == 0
    assert result.skipped_duplicates == 1
    assert repo.event_count() == 1


def test_upsert_upgrades_legacy_row_with_fill_id(temp_db):
    """Reimport with fill_id should upgrade legacy fingerprint row in place."""
    repo = create_repository()
    legacy = _sample_event(raw_ref="legacy::0")
    repo.upsert_events([legacy])
    existing_id = repo.list_events()[0].event_id

    keyed = _sample_event(fill_id="11718233245", raw_ref="keyed::0")
    result = repo.upsert_events([keyed])
    assert result.imported == 0
    assert result.skipped_duplicates == 0
    assert repo.event_count() == 1

    stored = repo.list_events()[0]
    assert stored.event_id == existing_id
    assert stored.fill_id == "11718233245"
    assert stored.natural_key == "tradovate|tradovate|11718233245"


def test_within_csv_batch_dedupes_duplicate_rows(temp_db):
    repo = create_repository()
    dupes = [_sample_event(raw_ref=f"batch::{i}") for i in range(3)]
    result = repo.upsert_events(dupes)
    assert result.imported == 1
    assert result.skipped_duplicates == 2
    assert repo.event_count() == 1


def test_overlapping_csv_reimport_is_idempotent(temp_db):
    path = FIXTURES / "robinhood_sample.csv"
    r1 = import_and_assemble(path, "robinhood", account="test")
    r2 = import_and_assemble(path, "robinhood", account="test")
    assert r1.imported == 4
    assert r2.imported == 0
    assert r2.skipped_duplicates == 4
    assert create_repository().event_count() == 4
