from dataclasses import dataclass

import pytest
from conftest import FakeAIClient, FakeAppriseClient, FakeFirecrawlClient

from feedsentry.domain import DecisionAction, EventStatus, ScreeningDecision
from feedsentry.processor import EventProcessor


@dataclass
class ProcessorFixture:
    repository: object
    processor: EventProcessor
    event_id: int
    ai: FakeAIClient
    firecrawl: FakeFirecrawlClient
    apprise: FakeAppriseClient


@pytest.fixture
async def processor_fixture(repository) -> ProcessorFixture:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="item-processor",
        title="Release V2",
        summary="Feed excerpt",
        link="https://example.com/release-v2",
        author=None,
        published_at=None,
        content_hash="entry-hash",
        raw_json="{}",
    )
    event_id = await repository.create_event(entry.id, "Important releases", "goal")
    ai = FakeAIClient()
    firecrawl = FakeFirecrawlClient()
    apprise = FakeAppriseClient()
    return ProcessorFixture(
        repository=repository,
        processor=EventProcessor(repository, ai, firecrawl, apprise, "telegram"),
        event_id=event_id,
        ai=ai,
        firecrawl=firecrawl,
        apprise=apprise,
    )


async def test_discard_finishes_without_delivery(processor_fixture: ProcessorFixture) -> None:
    fixture = processor_fixture
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.DISCARD, reason="outside goal"
    )

    await fixture.processor.process_event(fixture.event_id)

    event = await fixture.repository.get_event(fixture.event_id)
    assert event.status is EventStatus.FILTERED
    assert await fixture.repository.count_deliveries() == 0


async def test_fetch_path_caches_content_and_delivers(processor_fixture: ProcessorFixture) -> None:
    fixture = processor_fixture
    fixture.ai.screen_result = ScreeningDecision(action=DecisionAction.FETCH, reason="need details")
    fixture.ai.summary_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major capability",
        title="Release V2",
        summary="Adds durable workflows",
    )
    fixture.firecrawl.markdown = "Full release notes"

    await fixture.processor.process_event(fixture.event_id)

    event = await fixture.repository.get_event(fixture.event_id)
    assert event.status is EventStatus.DELIVERED
    assert fixture.firecrawl.calls == 1
    assert fixture.apprise.calls == 1
    assert await fixture.repository.count_deliveries() == 1


async def test_apprise_failure_retries_delivery_without_repeating_ai(
    processor_fixture: ProcessorFixture,
) -> None:
    fixture = processor_fixture
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="Release V2",
        summary="Adds durable workflows",
    )
    fixture.apprise.error = RuntimeError("temporary outage")

    await fixture.processor.process_event(fixture.event_id)

    waiting = await fixture.repository.get_event(fixture.event_id)
    assert waiting.status is EventStatus.RETRY_WAIT
    assert waiting.resume_stage is EventStatus.DELIVERING
    fixture.apprise.error = None
    await fixture.repository.make_event_due(fixture.event_id)
    await fixture.processor.process_event(fixture.event_id)

    delivered = await fixture.repository.get_event(fixture.event_id)
    assert delivered.status is EventStatus.DELIVERED
    assert fixture.ai.screen_calls == 1
    assert fixture.apprise.calls == 2


async def test_delivery_uses_global_destination(processor_fixture: ProcessorFixture) -> None:
    fixture = processor_fixture
    fixture.processor = EventProcessor(
        fixture.repository,
        fixture.ai,
        fixture.firecrawl,
        fixture.apprise,
        "global-destination",
    )
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="Release V2",
        summary="Adds durable workflows",
    )

    await fixture.processor.process_event(fixture.event_id)

    assert fixture.apprise.notifications[0][0] == "global-destination"


async def test_delivery_reads_current_global_destination(
    processor_fixture: ProcessorFixture,
) -> None:
    fixture = processor_fixture
    current = {"key": "updated-destination"}
    fixture.processor = EventProcessor(
        fixture.repository,
        fixture.ai,
        fixture.firecrawl,
        fixture.apprise,
        lambda: current["key"],
    )
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="Release V2",
        summary="Adds durable workflows",
    )

    await fixture.processor.process_event(fixture.event_id)

    assert fixture.apprise.notifications[0][0] == "updated-destination"
