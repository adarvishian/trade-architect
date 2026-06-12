"""Application bootstrap — wire adapters, repo, services."""

from __future__ import annotations

from pathlib import Path

from trading_architect.assembly.positions import assemble_positions, enrich_positions_with_marks
from trading_architect.config.defaults import DB_PATH
from trading_architect.config.user_settings import AppSettings
from trading_architect.engines.book_context import BookContext, build_book_context
from trading_architect.engines.marks import MarksProvider, NullMarksProvider, default_marks_provider
from trading_architect.ingestion.base import IngestionService, resolve_epoch_id
from trading_architect.ingestion.robinhood import RobinhoodAdapter
from trading_architect.ingestion.schwab import SchwabAdapter
from trading_architect.ingestion.tradovate import TradovateAdapter
from trading_architect.models.entities import ImportResult, Silo, TradeEvent
from trading_architect.services.current_state import (
    current_positions,
    silo_equity_from_snapshots,
    silo_peak_equity_from_snapshots,
)
from trading_architect.store.database import Database
from trading_architect.store.repository import Repository


def create_repository(db_path: Path | None = None) -> Repository:
    db = Database(db_path or DB_PATH)
    return Repository(db)


def _snapshot_live_equity(
    repo: Repository,
    settings: AppSettings,
    silo: Silo,
) -> tuple[float | None, float | None]:
    """Return (live_equity, live_peak) when balance snapshots exist for the silo."""
    accounts = repo.list_accounts(silo=silo)
    if not accounts:
        return None, None
    equity, as_of = silo_equity_from_snapshots(repo, silo, settings)
    if as_of is None:
        return None, None
    peak = silo_peak_equity_from_snapshots(repo, silo, settings)
    return equity, peak


def build_app_book_context(
    events: list[TradeEvent],
    positions: list,
    settings: AppSettings,
    marks_provider: MarksProvider | None = None,
    *,
    repo: Repository | None = None,
) -> BookContext:
    """Book context using persisted broker snapshots when available."""
    marks_provider = marks_provider if marks_provider is not None else default_marks_provider()
    repo = repo or create_repository()

    holdings_positions = current_positions(repo, marks_provider)
    book_positions = holdings_positions if holdings_positions else positions

    so_equity, so_peak = _snapshot_live_equity(repo, settings, Silo.STOCK_OPTIONS)
    fut_equity, fut_peak = _snapshot_live_equity(repo, settings, Silo.FUTURES)

    return build_book_context(
        events,
        book_positions,
        settings,
        marks_provider,
        live_stock_options_equity=so_equity,
        live_futures_equity=fut_equity,
        live_stock_options_peak=so_peak,
        live_futures_peak=fut_peak,
    )


def create_ingestion_service(repo: Repository | None = None) -> IngestionService:
    repo = repo or create_repository()
    service = IngestionService(repo)
    service.register(RobinhoodAdapter())
    service.register(SchwabAdapter())
    service.register(TradovateAdapter())
    return service


def _assemble_and_enrich(
    events: list[TradeEvent],
    marks_provider: MarksProvider | None = None,
) -> list:
    marks_provider = marks_provider if marks_provider is not None else default_marks_provider()
    positions = assemble_positions(events)
    if isinstance(marks_provider, NullMarksProvider):
        return positions
    return enrich_positions_with_marks(positions, marks_provider, events)


def _finalize_import(
    repo: Repository,
    result: ImportResult,
    marks_provider: MarksProvider | None = None,
) -> ImportResult:
    """Post-import dedup safety net and position reassembly."""
    removed_dupes = repo.remove_content_duplicate_events()
    if removed_dupes:
        result.skipped_duplicates += removed_dupes

    removed_invalid = repo.remove_events_with_invalid_underlying()
    if removed_invalid:
        result.skipped_duplicates += removed_invalid

    events = repo.list_events()
    positions = _assemble_and_enrich(events, marks_provider)
    repo.replace_positions(positions)
    return result


def import_and_assemble(
    path: Path,
    broker: str,
    account: str | None = None,
    repo: Repository | None = None,
    marks_provider: MarksProvider | None = None,
) -> ImportResult:
    repo = repo or create_repository()
    ingestion = create_ingestion_service(repo)
    result = ingestion.import_csv(path, broker=broker, account=account)
    return _finalize_import(repo, result, marks_provider)


def import_robinhood_fetch(
    account: str = "robinhood-roth",
    username: str | None = None,
    password: str | None = None,
    mfa_code: str | None = None,
    save_csv: bool = True,
    repo: Repository | None = None,
) -> ImportResult:
    """Fetch Robinhood history via API, persist events, and reassemble positions."""
    from datetime import datetime

    from trading_architect.config.defaults import DATA_DIR
    from trading_architect.ingestion.robinhood_fetch import fetch_all, save_events_as_csv

    repo = repo or create_repository()
    events, review_items = fetch_all(
        account=account,
        username=username,
        password=password,
        mfa_code=mfa_code,
    )

    if save_csv and events:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_events_as_csv(events, DATA_DIR / "raw" / f"{ts}_{account}_api.csv")

    for event in events:
        event.epoch_id = resolve_epoch_id(event.timestamp.date())

    review_count = repo.add_review_items(review_items)
    result = repo.upsert_events(events)
    result.review_queue = review_count
    result.source_file = f"robinhood-api::{account}"
    return _finalize_import(repo, result)


def import_schwab_fetch(
    account: str = "schwab-tos",
    start=None,
    end=None,
    save_csv: bool = True,
    repo: Repository | None = None,
    client=None,
) -> ImportResult:
    """Fetch Schwab history via API, persist events, and reassemble positions."""
    from datetime import datetime

    from trading_architect.config.defaults import DATA_DIR
    from trading_architect.ingestion.schwab_transactions import fetch_all

    repo = repo or create_repository()
    events, review_items = fetch_all(account=account, start=start, end=end, client=client)

    if save_csv and events:
        from trading_architect.ingestion.robinhood_fetch import save_events_as_csv

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_events_as_csv(events, DATA_DIR / "raw" / f"{ts}_{account}_schwab_api.csv")

    for event in events:
        event.epoch_id = resolve_epoch_id(event.timestamp.date())

    review_count = repo.add_review_items(review_items)
    result = repo.upsert_events(events)
    result.review_queue = review_count
    result.source_file = f"schwab-api::{account}"
    return _finalize_import(repo, result)


def fetch_schwab_portfolio_snapshot(
    labels: list[str] | None = None,
    *,
    repo: Repository | None = None,
    client=None,
):
    """Fetch Schwab balances/positions and persist snapshots."""
    from trading_architect.ingestion.schwab_accounts import fetch_portfolio_snapshot

    repo = repo or create_repository()
    return fetch_portfolio_snapshot(labels, client=client, repo=repo)
