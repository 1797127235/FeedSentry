from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from conftest import FakeAIClient, FakeAppriseClient, FakeFirecrawlClient

from feedsentry.config.models import DestinationConfig
from feedsentry.core.domain import DecisionAction, EventStatus, Notification, ScreeningDecision
from feedsentry.pipeline.processor import EventProcessor


@dataclass
class ProcessorFixture:
    repository: object
    processor: EventProcessor
    event_id: int
    ai: FakeAIClient
    firecrawl: FakeFirecrawlClient
    apprise: FakeAppriseClient


class FakeQQ:
    def __init__(self, destination_key: str = "qq:group:987") -> None:
        self.destination_key = destination_key
        self.calls: list[Notification] = []
        self.error: Exception | None = None

    async def notify(self, notification: Notification) -> str:
        self.calls.append(notification)
        if self.error is not None:
            raise self.error
        return "qq_message_id=42"


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


async def test_recovery_does_not_repeat_a_recorded_successful_delivery(
    processor_fixture: ProcessorFixture,
) -> None:
    fixture = processor_fixture
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="Release V2",
        summary="Adds durable workflows",
    )
    await fixture.repository.transition_event(
        fixture.event_id, EventStatus.DISCOVERED, EventStatus.SCREENING
    )
    await fixture.repository.transition_event(
        fixture.event_id,
        EventStatus.SCREENING,
        EventStatus.DELIVERY_PENDING,
        decision_reason="major release",
        output_title="Release V2",
        output_summary="Adds durable workflows",
    )
    await fixture.repository.transition_event(
        fixture.event_id, EventStatus.DELIVERY_PENDING, EventStatus.DELIVERING
    )
    delivery = await fixture.repository.create_delivery(fixture.event_id, "telegram")
    await fixture.repository.mark_delivery_success(delivery.id, "sent")

    await fixture.processor.process_event(fixture.event_id)

    event = await fixture.repository.get_event(fixture.event_id)
    assert event.status is EventStatus.DELIVERED
    assert fixture.apprise.calls == 0


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


async def test_delivery_delivers_via_qq_client(processor_fixture: ProcessorFixture) -> None:
    fixture = processor_fixture
    qq = FakeQQ()
    fixture.processor = EventProcessor(
        fixture.repository,
        fixture.ai,
        fixture.firecrawl,
        fixture.apprise,
        DestinationConfig(kind="qq"),
        qq=qq,
    )
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="Release V2",
        summary="Adds durable workflows",
    )

    await fixture.processor.process_event(fixture.event_id)

    event = await fixture.repository.get_event(fixture.event_id)
    assert event.status is EventStatus.DELIVERED
    assert len(qq.calls) == 1
    assert qq.calls[0].link == "https://example.com/release-v2"
    deliveries = await fixture.repository.list_deliveries_for_event(fixture.event_id)
    assert deliveries[0].apprise_key == "qq:group:987"
    assert deliveries[0].status == "delivered"
    assert deliveries[0].response_summary == "qq_message_id=42"


async def test_delivery_includes_feed_title_in_notification(
    processor_fixture: ProcessorFixture,
) -> None:
    fixture = processor_fixture
    now = datetime.now(UTC)
    await fixture.repository.record_feed_success(
        "https://example.com/feed",
        etag=None,
        last_modified=None,
        checked_at=now,
        next_check_at=now,
        title="Example Feed",
    )
    qq = FakeQQ()
    fixture.processor = EventProcessor(
        fixture.repository,
        fixture.ai,
        fixture.firecrawl,
        fixture.apprise,
        DestinationConfig(kind="qq"),
        qq=qq,
    )
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="Release V2",
        summary="Adds durable workflows",
    )

    await fixture.processor.process_event(fixture.event_id)

    assert len(qq.calls) == 1
    assert qq.calls[0].source_title == "Example Feed"


async def test_qq_failure_retries_without_repeating_delivery_record(
    processor_fixture: ProcessorFixture,
) -> None:
    fixture = processor_fixture
    qq = FakeQQ()
    qq.error = RuntimeError("napcat down")
    fixture.processor = EventProcessor(
        fixture.repository,
        fixture.ai,
        fixture.firecrawl,
        fixture.apprise,
        DestinationConfig(kind="qq"),
        qq=qq,
    )
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="Release V2",
        summary="Adds durable workflows",
    )

    await fixture.processor.process_event(fixture.event_id)

    waiting = await fixture.repository.get_event(fixture.event_id)
    assert waiting.status is EventStatus.RETRY_WAIT
    assert waiting.resume_stage is EventStatus.DELIVERING
    assert await fixture.repository.count_deliveries() == 1

    qq.error = None
    await fixture.repository.make_event_due(fixture.event_id)
    await fixture.processor.process_event(fixture.event_id)

    delivered = await fixture.repository.get_event(fixture.event_id)
    assert delivered.status is EventStatus.DELIVERED
    assert len(qq.calls) == 2
    assert await fixture.repository.count_deliveries() == 1
