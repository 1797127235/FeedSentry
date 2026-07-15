from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from conftest import FakeAIClient, FakeAppriseClient, FakeFirecrawlClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_config import VALID_CONFIG

from feedsentry.config import ConfigManager
from feedsentry.config_store import ConfigStore
from feedsentry.control import SourceService, StatusService
from feedsentry.domain import DecisionAction, EventStatus, ScreeningDecision
from feedsentry.feed_validation import ValidatedFeed
from feedsentry.feeds import FeedFetchResult, NormalizedEntry
from feedsentry.ingestion import IngestionService
from feedsentry.mcp import ControlServices, create_mcp_app
from feedsentry.polling import PollCoordinator
from feedsentry.processor import EventProcessor
from feedsentry.rsshub import CandidateCodec


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


async def test_new_entry_is_enriched_and_delivered_once(repository, fake_feed_client) -> None:
    ingestion = IngestionService(repository, fake_feed_client)
    fake_feed_client.result = FeedFetchResult(False, "one", None, (entry("old"),))
    assert await ingestion.poll_source("https://example.com/feed", "Important releases") == 0

    fake_feed_client.result = FeedFetchResult(False, "two", None, (entry("old"), entry("new")))
    assert await ingestion.poll_source("https://example.com/feed", "Important releases") == 1
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


class EndToEndValidator:
    async def validate(self, url: str) -> ValidatedFeed:
        return ValidatedFeed(
            canonical_url=url,
            title="Example",
            version="rss20",
            etag="one",
            last_modified=None,
            entries=(entry("old"),),
        )


class UnusedRSSHub:
    async def rules(self):
        raise AssertionError("RSSHub should not be called for a direct feed")


async def test_mcp_add_feed_is_silent_then_new_item_is_delivered_once(
    tmp_path, monkeypatch, repository, fake_feed_client
) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")
    manager = ConfigManager(config_path)
    manager.load_initial()
    ingestion = IngestionService(repository, fake_feed_client)
    polling = PollCoordinator(repository, ingestion)
    sources = SourceService(
        manager,
        ConfigStore(manager),
        repository,
        EndToEndValidator(),
        UnusedRSSHub(),
        CandidateCodec(b"secret"),
        polling,
    )
    app = create_mcp_app(
        ControlServices(sources=sources, status=StatusService(manager, repository)),
        token="secret",
        allowed_hosts=["localhost"],
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer secret"},
    )
    async with app.router.lifespan_context(app), http:
        async with streamable_http_client("http://localhost/", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool("add_feed", {"url": "https://example.com/feed"})

    payload = json.loads(result.content[0].text)
    assert payload["created"] is True
    assert payload["baseline_initialized"] is True
    assert await repository.count_events() == 0

    fake_feed_client.result = FeedFetchResult(False, "two", None, (entry("old"), entry("new")))
    assert await ingestion.poll_source("https://example.com/feed", "Important releases") == 1
    event_id = (await repository.list_due_event_ids(datetime.now(UTC), 20))[0]
    ai = FakeAIClient()
    ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="important",
        title="New",
        summary="Important update",
    )
    apprise = FakeAppriseClient()
    processor = EventProcessor(repository, ai, FakeFirecrawlClient(), apprise, "telegram")

    await processor.process_event(event_id)

    assert (await repository.get_event(event_id)).status is EventStatus.DELIVERED
    assert ai.screen_calls == 1
    assert apprise.calls == 1
