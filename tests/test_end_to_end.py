from __future__ import annotations

from datetime import UTC, datetime

from conftest import FakeAIClient, FakeAppriseClient, FakeFirecrawlClient

from feedsentry.domain import DecisionAction, EventStatus, ScreeningDecision
from feedsentry.feeds import FeedFetchResult, NormalizedEntry
from feedsentry.ingestion import IngestionService
from feedsentry.processor import EventProcessor


def entry(identifier: str) -> NormalizedEntry:
    return NormalizedEntry(
        source_url="https://example.com/feed",
        external_id=identifier,
        title=identifier,
        summary="summary",
        link=f"https://example.com/{identifier}",
        author=None,
        published_at=None,
        content_hash=f"hash-{identifier}",
        raw_json="{}",
    )


async def test_new_entry_is_enriched_and_delivered_once(
    repository, fake_feed_client, make_monitor
) -> None:
    monitor = make_monitor()
    ingestion = IngestionService(repository, fake_feed_client)
    fake_feed_client.result = FeedFetchResult(False, "one", None, (entry("old"),))
    assert await ingestion.poll_monitor_source(monitor, "https://example.com/feed") == 0

    fake_feed_client.result = FeedFetchResult(False, "two", None, (entry("old"), entry("new")))
    assert await ingestion.poll_monitor_source(monitor, "https://example.com/feed") == 1
    event_id = (await repository.list_due_event_ids(datetime.now(UTC), 20))[0]

    ai = FakeAIClient()
    ai.screen_result = ScreeningDecision(action=DecisionAction.FETCH, reason="need details")
    ai.summary_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="New release",
        summary="Adds durable workflows",
    )
    firecrawl = FakeFirecrawlClient()
    firecrawl.markdown = "Full notes"
    apprise = FakeAppriseClient()
    processor = EventProcessor(repository, ai, firecrawl, apprise, "telegram")

    await processor.process_event(event_id)
    await processor.process_event(event_id)

    assert (await repository.get_event(event_id)).status is EventStatus.DELIVERED
    assert firecrawl.calls == 1
    assert ai.screen_calls == 1
    assert ai.summary_calls == 1
    assert apprise.calls == 1
