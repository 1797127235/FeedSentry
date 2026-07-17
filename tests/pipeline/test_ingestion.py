from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from feedsentry.clients.feeds import FeedFetchResult, NormalizedEntry
from feedsentry.pipeline.ingestion import IngestionService


def entry(external_id: str) -> NormalizedEntry:
    return NormalizedEntry(
        source_url="https://example.com/feed",
        external_id=external_id,
        title=external_id,
        summary="summary",
        link=f"https://example.com/{external_id}",
        author=None,
        published_at=None,
        content_hash=f"hash-{external_id}",
        raw_json="{}",
    )


async def test_first_fetch_creates_baseline_without_events(repository, fake_feed_client) -> None:
    fake_feed_client.result = FeedFetchResult(False, "etag", None, (entry("old"),))

    created = await IngestionService(repository, fake_feed_client).poll_source(
        "https://example.com/feed", "Important releases"
    )

    assert created == 0
    assert await repository.count_events() == 0
    state = await repository.get_feed_state("https://example.com/feed")
    assert state is not None
    assert state.initialized_at is not None
    assert state.etag == "etag"


async def test_fetch_persists_feed_title(repository, fake_feed_client) -> None:
    fake_feed_client.result = FeedFetchResult(
        False, "etag", None, (entry("old"),), title="Example Feed"
    )

    await IngestionService(repository, fake_feed_client).poll_source(
        "https://example.com/feed", "Important releases"
    )

    state = await repository.get_feed_state("https://example.com/feed")
    assert state is not None
    assert state.title == "Example Feed"


async def test_later_fetch_creates_one_event_per_new_entry(repository, fake_feed_client) -> None:
    service = IngestionService(repository, fake_feed_client)
    fake_feed_client.result = FeedFetchResult(False, "e1", None, (entry("old"),))
    await service.poll_source("https://example.com/feed", "Important releases")
    fake_feed_client.result = FeedFetchResult(False, "e2", None, (entry("old"), entry("new")))

    created = await service.poll_source("https://example.com/feed", "Important releases")

    assert created == 1
    assert await repository.count_events() == 1
    assert fake_feed_client.calls[1] == ("https://example.com/feed", "e1", None)


async def test_not_modified_records_success_without_events(repository, fake_feed_client) -> None:
    service = IngestionService(repository, fake_feed_client)
    fake_feed_client.result = FeedFetchResult(False, "e1", "yesterday", (entry("old"),))
    await service.poll_source("https://example.com/feed", "Important releases")
    fake_feed_client.result = FeedFetchResult(True, None, None, ())

    created = await service.poll_source("https://example.com/feed", "Important releases")

    assert created == 0
    assert await repository.count_events() == 0
    state = await repository.get_feed_state("https://example.com/feed")
    assert state is not None
    assert state.etag == "e1"
    assert state.last_modified == "yesterday"
    assert state.consecutive_failures == 0


async def test_http_error_records_bounded_failure_and_returns_zero(
    repository, fake_feed_client
) -> None:
    now = datetime.now(UTC)
    await repository.record_feed_success(
        "https://example.com/feed",
        etag=None,
        last_modified=None,
        checked_at=now,
        next_check_at=now,
    )
    fake_feed_client.error = httpx.ConnectError("x" * 2_000)

    created = await IngestionService(repository, fake_feed_client).poll_source(
        "https://example.com/feed", "Important releases"
    )

    assert created == 0
    state = await repository.get_feed_state("https://example.com/feed")
    assert state is not None
    assert state.consecutive_failures == 1
    assert state.last_error is not None
    assert len(state.last_error) <= 1_000
    assert state.next_check_at is not None
    assert timedelta(seconds=59) <= state.next_check_at - now <= timedelta(minutes=2)


async def test_failure_backoff_caps_at_two_hours(repository) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    for attempt, delay in enumerate((1, 5, 30, 120, 120), start=1):
        await repository.record_feed_failure(
            "https://example.com/feed",
            error="failed",
            checked_at=now,
            next_check_at=now + timedelta(minutes=delay),
        )
        state = await repository.get_feed_state("https://example.com/feed")
        assert state is not None
        assert state.consecutive_failures == attempt
        assert state.next_check_at == now + timedelta(minutes=delay)
