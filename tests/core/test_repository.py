import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from feedsentry.core.database import DeliveryRow, EventRow, create_database
from feedsentry.core.domain import EventStatus
from feedsentry.core.repository import Repository


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
    invalid_event = insert(EventRow).values(
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


async def test_feed_baseline_is_scoped_to_source(repository: Repository) -> None:
    now = datetime.now(UTC)

    await repository.mark_feed_initialized("https://example.com/feed", now)

    assert await repository.feed_is_initialized("https://example.com/feed")
    assert not await repository.feed_is_initialized("https://example.com/other")


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
    assert {"feed_state", "entries", "events", "scrape_cache", "deliveries"} <= tables
    assert "monitor_events" not in tables


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

    first = await repository.create_event(entry.id, "goal", "goal-hash")
    second = await repository.create_event(entry.id, "goal", "goal-hash")

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
    event_id = await repository.create_event(entry.id, "goal", "goal-hash")

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
    event_id = await repository.create_event(entry.id, "goal", "goal-hash")

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
        "https://example.com/feed",
        etag='"v1"',
        last_modified="Fri, 11 Jul 2026 00:00:00 GMT",
        checked_at=checked_at,
        next_check_at=next_check_at,
    )

    state = await repository.get_feed_state("https://example.com/feed")
    assert state is not None
    assert state.etag == '"v1"'
    assert state.last_modified == "Fri, 11 Jul 2026 00:00:00 GMT"
    assert state.last_success_at == checked_at
    assert state.consecutive_failures == 0
    assert not await repository.source_is_due("https://example.com/feed", checked_at)
    assert await repository.source_is_due("https://example.com/feed", next_check_at)
    assert await repository.source_is_due(
        "https://example.com/feed", next_check_at + timedelta(microseconds=1)
    )


async def test_feed_failure_increments_failures_and_preserves_validators(
    repository: Repository,
) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    await repository.record_feed_success(
        "https://example.com/feed",
        etag='"v1"',
        last_modified="Fri, 11 Jul 2026 00:00:00 GMT",
        checked_at=now,
        next_check_at=now,
    )
    await repository.record_feed_failure(
        "https://example.com/feed",
        error="timeout",
        checked_at=now,
        next_check_at=now + timedelta(minutes=1),
    )

    state = await repository.get_feed_state("https://example.com/feed")
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


async def test_terminal_failure_preserves_stage_and_can_be_retried(repository: Repository) -> None:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="failed-item",
        title="Failed",
        summary="Summary",
        link="https://example.com/failed",
        author=None,
        published_at=None,
        content_hash="failed-hash",
        raw_json="{}",
    )
    event_id = await repository.create_event(entry.id, "goal", "goal-hash")
    await repository.transition_event(event_id, EventStatus.DISCOVERED, EventStatus.SCREENING)
    for _ in range(4):
        await repository.schedule_event_retry(event_id, EventStatus.SCREENING, "AI unavailable")
        await repository.make_event_due(event_id)
        await repository.resume_event(event_id)
    await repository.schedule_event_retry(event_id, EventStatus.SCREENING, "AI unavailable")

    failed = await repository.get_event(event_id)
    assert failed.status is EventStatus.FAILED
    assert failed.resume_stage is EventStatus.SCREENING

    assert await repository.retry_failed_event(event_id) is True
    waiting = await repository.get_event(event_id)
    assert waiting.status is EventStatus.RETRY_WAIT
    assert waiting.resume_stage is EventStatus.SCREENING
    assert waiting.next_attempt_at is not None


async def test_list_events_filters_and_paginates(repository: Repository) -> None:
    now = datetime.now(UTC)
    entry_a = await repository.upsert_entry(
        source_url="https://example.com/a.xml",
        external_id="1",
        title="Alpha release",
        summary="summary a",
        link="https://example.com/a/1",
        author=None,
        published_at=now,
        content_hash="h1",
        raw_json="{}",
    )
    entry_b = await repository.upsert_entry(
        source_url="https://example.com/b.xml",
        external_id="2",
        title="Beta noise",
        summary="summary b",
        link="https://example.com/b/2",
        author=None,
        published_at=now,
        content_hash="h2",
        raw_json="{}",
    )
    event_a = await repository.create_event(entry_a.id, "goal", "ghash")
    event_b = await repository.create_event(entry_b.id, "goal", "ghash")
    await repository.transition_event(event_a, EventStatus.DISCOVERED, EventStatus.SCREENING)
    await repository.transition_event(
        event_a,
        EventStatus.SCREENING,
        EventStatus.FILTERED,
        decision_reason="not relevant",
    )
    await repository.transition_event(event_b, EventStatus.DISCOVERED, EventStatus.SCREENING)
    await repository.transition_event(
        event_b,
        EventStatus.SCREENING,
        EventStatus.DELIVERY_PENDING,
        decision_reason="ship it",
        output_title="Beta",
        output_summary="important",
    )

    filtered, cursor = await repository.list_events(
        status=EventStatus.FILTERED.value, source_url=None, q=None, limit=10, cursor=None
    )
    assert len(filtered) == 1
    assert filtered[0].event_id == event_a
    assert filtered[0].decision_reason == "not relevant"
    assert filtered[0].title == "Alpha release"
    assert filtered[0].source_url == "https://example.com/a.xml"
    assert filtered[0].created_at.tzinfo is UTC
    assert filtered[0].updated_at.tzinfo is UTC
    assert cursor is None

    searched, _ = await repository.list_events(
        status=None, source_url=None, q="Beta", limit=10, cursor=None
    )
    assert [item.event_id for item in searched] == [event_b]

    by_source, _ = await repository.list_events(
        status=None,
        source_url="https://example.com/a.xml",
        q=None,
        limit=10,
        cursor=None,
    )
    assert [item.event_id for item in by_source] == [event_a]

    page1, next_cursor = await repository.list_events(
        status=None, source_url=None, q=None, limit=1, cursor=None
    )
    assert len(page1) == 1
    assert next_cursor is not None
    page2, next2 = await repository.list_events(
        status=None, source_url=None, q=None, limit=1, cursor=next_cursor
    )
    assert len(page2) == 1
    assert page1[0].event_id != page2[0].event_id
    assert {page1[0].event_id, page2[0].event_id} == {event_a, event_b}
    assert next2 is None


async def test_status_breakdown_counts_by_status(repository: Repository) -> None:
    now = datetime.now(UTC)
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed.xml",
        external_id="x",
        title="T",
        summary="S",
        link="https://example.com/x",
        author=None,
        published_at=now,
        content_hash="hx",
        raw_json="{}",
    )
    event_id = await repository.create_event(entry.id, "goal", "ghash")
    await repository.transition_event(event_id, EventStatus.DISCOVERED, EventStatus.SCREENING)
    await repository.transition_event(
        event_id, EventStatus.SCREENING, EventStatus.FILTERED, decision_reason="nope"
    )
    breakdown = await repository.status_breakdown()
    assert breakdown.get(EventStatus.FILTERED.value, 0) >= 1


async def test_list_deliveries_for_event(repository: Repository) -> None:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="delivery-item",
        title="Deliver me",
        summary="Summary",
        link="https://example.com/delivery",
        author=None,
        published_at=None,
        content_hash="delivery-hash",
        raw_json="{}",
    )
    event_id = await repository.create_event(entry.id, "goal", "ghash")
    first = await repository.create_delivery(event_id, "telegram")
    second = await repository.create_delivery(event_id, "discord")
    await repository.mark_delivery_success(first.id, "ok")

    deliveries = await repository.list_deliveries_for_event(event_id)
    assert [item.id for item in deliveries] == [first.id, second.id]
    assert deliveries[0].status == "delivered"
    assert deliveries[1].status == "pending"
    assert deliveries[0].created_at.tzinfo is UTC
