import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from feedsentry.database import DeliveryRow, MonitorEventRow, create_database
from feedsentry.domain import EventStatus
from feedsentry.repository import Repository


@pytest.fixture
async def database(tmp_path):
    database = create_database(tmp_path / "feedsentry.db")
    await database.initialize()
    yield database
    await database.dispose()


@pytest.fixture
async def repository(database):
    return Repository(database.session_factory)


async def test_database_enforces_foreign_keys_on_every_connection(database) -> None:
    await database.engine.dispose()
    now = datetime.now(UTC)
    invalid_event = insert(MonitorEventRow).values(
        monitor_id="monitor-a",
        entry_id=999,
        status=EventStatus.DISCOVERED.value,
        goal_snapshot="goal",
        goal_hash="goal-hash",
        failure_count=0,
        created_at=now,
        updated_at=now,
    )
    invalid_delivery = insert(DeliveryRow).values(
        event_id=999,
        apprise_key="telegram",
        idempotency_key="idempotency-key",
        status="pending",
        attempts=0,
        created_at=now,
        updated_at=now,
    )

    for statement in (invalid_event, invalid_delivery):
        async with database.session_factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(statement)
                await session.commit()
            await session.rollback()


async def test_feed_baseline_is_scoped_to_monitor(repository: Repository) -> None:
    now = datetime.now(UTC)

    await repository.mark_feed_initialized("monitor-a", "https://example.com/feed", now)

    assert await repository.feed_is_initialized("monitor-a", "https://example.com/feed")
    assert not await repository.feed_is_initialized("monitor-b", "https://example.com/feed")


async def test_database_initializes_wal_and_all_storage_tables(tmp_path) -> None:
    path = tmp_path / "feedsentry.db"
    database = create_database(path)
    await database.initialize()
    await database.dispose()

    with sqlite3.connect(path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert journal_mode == "wal"
    assert {"feed_state", "entries", "monitor_events", "scrape_cache", "deliveries"} <= tables


async def test_event_insert_is_idempotent(repository: Repository) -> None:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="item-1",
        title="One",
        summary="Summary",
        link="https://example.com/1",
        author=None,
        published_at=None,
        content_hash="hash-1",
        raw_json="{}",
    )

    first = await repository.create_event("monitor-a", entry.id, "goal", "goal-hash")
    second = await repository.create_event("monitor-a", entry.id, "goal", "goal-hash")

    assert first == second


async def test_recovery_returns_in_progress_events_to_retry(repository: Repository) -> None:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="item-2",
        title="Two",
        summary="Summary",
        link="https://example.com/2",
        author=None,
        published_at=None,
        content_hash="hash-2",
        raw_json="{}",
    )
    event_id = await repository.create_event("monitor-a", entry.id, "goal", "goal-hash")

    await repository.transition_event(event_id, EventStatus.DISCOVERED, EventStatus.SCREENING)
    await repository.transition_event(event_id, EventStatus.SCREENING, EventStatus.FETCHING)

    await repository.recover_in_progress()
    event = await repository.get_event(event_id)

    assert event.status is EventStatus.RETRY_WAIT
    assert event.resume_stage is EventStatus.FETCHING
    assert event.next_attempt_at is not None
    assert event.next_attempt_at.tzinfo is UTC
    assert event.next_attempt_at <= datetime.now(UTC)


async def test_transition_does_not_overwrite_an_event_advanced_by_another_worker(
    repository: Repository,
) -> None:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="item-3",
        title="Three",
        summary="Summary",
        link="https://example.com/3",
        author=None,
        published_at=None,
        content_hash="hash-3",
        raw_json="{}",
    )
    event_id = await repository.create_event("monitor-a", entry.id, "goal", "goal-hash")

    assert await repository.transition_event(
        event_id, EventStatus.DISCOVERED, EventStatus.SCREENING
    )
    assert not await repository.transition_event(
        event_id, EventStatus.DISCOVERED, EventStatus.SCREENING
    )


async def test_feed_success_persists_validators_and_due_time(repository: Repository) -> None:
    checked_at = datetime(2026, 7, 11, tzinfo=UTC)
    next_check_at = checked_at + timedelta(minutes=10)

    await repository.record_feed_success(
        "monitor-a",
        "https://example.com/feed",
        etag='"v1"',
        last_modified="Fri, 11 Jul 2026 00:00:00 GMT",
        checked_at=checked_at,
        next_check_at=next_check_at,
    )

    state = await repository.get_feed_state("monitor-a", "https://example.com/feed")
    assert state is not None
    assert state.etag == '"v1"'
    assert state.last_modified == "Fri, 11 Jul 2026 00:00:00 GMT"
    assert state.last_success_at == checked_at
    assert state.consecutive_failures == 0
    assert not await repository.source_is_due("monitor-a", "https://example.com/feed", checked_at)
    assert await repository.source_is_due("monitor-a", "https://example.com/feed", next_check_at)
    assert await repository.source_is_due(
        "monitor-a", "https://example.com/feed", next_check_at + timedelta(microseconds=1)
    )


async def test_feed_failure_increments_failures_and_preserves_validators(
    repository: Repository,
) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    await repository.record_feed_success(
        "monitor-a",
        "https://example.com/feed",
        etag='"v1"',
        last_modified="Fri, 11 Jul 2026 00:00:00 GMT",
        checked_at=now,
        next_check_at=now,
    )
    await repository.record_feed_failure(
        "monitor-a",
        "https://example.com/feed",
        error="timeout",
        checked_at=now,
        next_check_at=now + timedelta(minutes=1),
    )

    state = await repository.get_feed_state("monitor-a", "https://example.com/feed")
    assert state is not None
    assert state.etag == '"v1"'
    assert state.last_modified == "Fri, 11 Jul 2026 00:00:00 GMT"
    assert state.consecutive_failures == 1
    assert state.last_error == "timeout"


async def test_upsert_entry_returns_original_first_seen_at(repository: Repository) -> None:
    first = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="item-1",
        title="One",
        summary="Summary",
        link="https://example.com/1",
        author=None,
        published_at=None,
        content_hash="hash-1",
        raw_json="{}",
    )
    second = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="item-1",
        title="One revised",
        summary="Updated summary",
        link="https://example.com/1",
        author="Author",
        published_at=None,
        content_hash="hash-2",
        raw_json='{"updated": true}',
    )

    assert second.id == first.id
    assert second.first_seen_at == first.first_seen_at
