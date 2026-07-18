from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from conftest import VALID_CONFIG

from feedsentry.clients.feed_validation import ValidatedFeed
from feedsentry.clients.feeds import NormalizedEntry
from feedsentry.clients.rsshub import CandidateCodec
from feedsentry.config.models import ConfigManager, DestinationConfig
from feedsentry.config.store import ConfigStore
from feedsentry.core.domain import EventStatus, Notification
from feedsentry.interfaces.control import (
    DestinationService,
    FilterService,
    RecoveryService,
    SourceService,
    StatusService,
)


def normalized_entry(source_url: str) -> NormalizedEntry:
    return NormalizedEntry(
        source_url=source_url,
        external_id="old",
        title="Old item",
        summary="Summary",
        link="https://example.com/old",
        author=None,
        published_at=None,
        content_hash="hash",
        raw_json="{}",
    )


class FakeValidator:
    async def validate(self, url: str) -> ValidatedFeed:
        return ValidatedFeed(
            canonical_url=url,
            title="Example Feed",
            version="atom10",
            etag='"one"',
            last_modified=None,
            entries=(normalized_entry(url),),
        )


class LongTitleValidator(FakeValidator):
    async def validate(self, url: str) -> ValidatedFeed:
        validated = await super().validate(url)
        return ValidatedFeed(
            canonical_url=validated.canonical_url,
            title="A" * 80,
            version=validated.version,
            etag=validated.etag,
            last_modified=validated.last_modified,
            entries=validated.entries,
        )


class ConcurrentValidator(FakeValidator):
    def __init__(self) -> None:
        self.arrived = 0
        self.ready = asyncio.Event()

    async def validate(self, url: str) -> ValidatedFeed:
        self.arrived += 1
        if self.arrived == 2:
            self.ready.set()
        await self.ready.wait()
        return await super().validate(url)


class BaselineCheckingStore(ConfigStore):
    def __init__(self, manager, repository) -> None:
        super().__init__(manager)
        self.repository = repository

    async def add_source(self, source) -> bool:
        assert await self.repository.feed_is_initialized(str(source.url))
        return await super().add_source(source)


class FakeRSSHub:
    base_url = "https://rsshub.antest.cc.cd"

    async def rules(self):
        return {
            "bilibili.com": {
                "_name": "Bilibili",
                "space": [
                    {
                        "title": "UP 主视频",
                        "source": ["/:uid"],
                        "target": "/bilibili/user/video/:uid",
                    }
                ],
            }
        }


class FakePolling:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def poll(self, source, goal, *, rsshub, force=False):
        del goal, rsshub, force
        self.calls.append(source.id)
        return 1


@pytest.fixture
def config_manager(tmp_path, monkeypatch) -> ConfigManager:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    manager = ConfigManager(path)
    manager.load_initial()
    return manager


@pytest.fixture
def source_service(config_manager, repository) -> SourceService:
    return SourceService(
        config_manager,
        ConfigStore(config_manager),
        repository,
        FakeValidator(),
        FakeRSSHub(),
        CandidateCodec(b"secret"),
        FakePolling(),
    )


async def test_add_direct_feed_builds_silent_baseline(source_service, repository) -> None:
    result = await source_service.add_feed("https://news.example/feed.xml")

    assert result.created is True
    assert result.baseline_initialized is True
    assert result.source.title == "Example Feed"
    assert await repository.count_events() == 0
    state = await repository.get_feed_state("https://news.example/feed.xml")
    assert state is not None and state.initialized_at is not None


async def test_add_direct_feed_builds_baseline_before_publishing_source(
    config_manager, repository
) -> None:
    service = SourceService(
        config_manager,
        BaselineCheckingStore(config_manager, repository),
        repository,
        FakeValidator(),
        FakeRSSHub(),
        CandidateCodec(b"secret"),
        FakePolling(),
    )

    result = await service.add_feed("https://news.example/feed.xml")

    assert result.created is True


async def test_add_direct_feed_is_idempotent(source_service) -> None:
    first = await source_service.add_feed("https://news.example/feed.xml")
    second = await source_service.add_feed("https://news.example/feed.xml")
    assert first.source.id == second.source.id
    assert second.created is False


async def test_different_feeds_with_same_long_title_get_unique_ids(
    config_manager, repository
) -> None:
    service = SourceService(
        config_manager,
        ConfigStore(config_manager),
        repository,
        LongTitleValidator(),
        FakeRSSHub(),
        CandidateCodec(b"secret"),
        FakePolling(),
    )

    first = await service.add_feed("https://one.example/feed.xml")
    second = await service.add_feed("https://two.example/feed.xml")

    assert first.created is True
    assert second.created is True
    assert first.source.id != second.source.id


async def test_concurrent_feeds_with_same_title_get_unique_ids(config_manager, repository) -> None:
    service = SourceService(
        config_manager,
        ConfigStore(config_manager),
        repository,
        ConcurrentValidator(),
        FakeRSSHub(),
        CandidateCodec(b"secret"),
        FakePolling(),
    )

    first, second = await asyncio.gather(
        service.add_feed("https://one.example/feed.xml"),
        service.add_feed("https://two.example/feed.xml"),
    )

    assert first.created is True
    assert second.created is True
    assert first.source.id != second.source.id


async def test_discover_and_subscribe_rsshub_candidate(source_service) -> None:
    candidates = await source_service.discover_feeds("https://space.bilibili.com/946974")
    assert candidates[0].title == "UP 主视频"

    result = await source_service.subscribe_feed(candidates[0].candidate_id)

    assert result.created is True
    assert result.source.kind == "rsshub"
    assert result.source.route == "/bilibili/user/video/946974"


async def test_manage_and_check_source(source_service) -> None:
    added = await source_service.add_feed("https://news.example/feed.xml")
    assert await source_service.set_enabled(added.source.id, False) is True
    assert (await source_service.list_sources())[1].enabled is False
    assert await source_service.check_now(added.source.id) == 1
    assert await source_service.remove(added.source.id) is True


async def test_list_sources_shows_persisted_feed_title(source_service) -> None:
    added = await source_service.add_feed("https://news.example/feed.xml")

    sources = {source.id: source for source in await source_service.list_sources()}

    assert sources[added.source.id].title == "Example Feed"


async def test_filter_service_reads_and_updates_goal(config_manager) -> None:
    service = FilterService(config_manager, ConfigStore(config_manager))
    assert service.get_goal() == "Important releases only"
    assert await service.set_goal("Security releases only") is True
    assert service.get_goal() == "Security releases only"


async def test_filter_service_appends_goal(config_manager) -> None:
    service = FilterService(config_manager, ConfigStore(config_manager))
    assert await service.append_goal("Security updates") is True
    assert service.get_goal() == "Important releases only\nSecurity updates"
    assert await service.append_goal("Security updates") is False
    assert service.get_goal() == "Important releases only\nSecurity updates"


async def test_filter_service_append_rejects_blank(config_manager) -> None:
    service = FilterService(config_manager, ConfigStore(config_manager))
    with pytest.raises(ValueError):
        await service.append_goal("   ")


async def test_status_service_returns_source_health(config_manager, repository) -> None:
    await repository.record_feed_failure(
        "https://example.com/feed.xml",
        error="timeout",
        checked_at=datetime.now(UTC),
        next_check_at=datetime.now(UTC),
    )
    tick = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    status = await StatusService(
        config_manager, repository, last_tick_provider=lambda: tick
    ).get_status()

    assert status.sources == 1
    assert status.enabled_sources == 1
    assert status.source_statuses[0].consecutive_failures == 1
    assert status.source_statuses[0].last_error == "timeout"
    assert status.last_tick_at == tick
    assert status.status_counts == {}


async def test_status_service_lists_events_with_source_id(config_manager, repository) -> None:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed.xml",
        external_id="list-control",
        title="Listed item",
        summary="Summary",
        link="https://example.com/list-control",
        author=None,
        published_at=None,
        content_hash="list-control-hash",
        raw_json="{}",
    )
    event_id = await repository.create_event(entry.id, "goal", "goal-hash")

    service = StatusService(config_manager, repository, last_tick_provider=lambda: None)
    views, cursor = await service.list_events(
        status=None, source_id=None, q=None, limit=10, cursor=None
    )

    assert cursor is None
    assert len(views) == 1
    assert views[0].event_id == event_id
    assert views[0].entry_id == entry.id
    assert views[0].status == EventStatus.DISCOVERED.value
    assert views[0].title == "Listed item"
    assert views[0].link == "https://example.com/list-control"
    assert views[0].source_url == "https://example.com/feed.xml"
    assert views[0].source_id == "example"
    assert views[0].created_at.tzinfo is UTC
    assert views[0].updated_at.tzinfo is UTC


async def test_status_service_list_events_filters_by_source_id(config_manager, repository) -> None:
    entry_known = await repository.upsert_entry(
        source_url="https://example.com/feed.xml",
        external_id="known",
        title="Known",
        summary="S",
        link="https://example.com/known",
        author=None,
        published_at=None,
        content_hash="known-hash",
        raw_json="{}",
    )
    entry_other = await repository.upsert_entry(
        source_url="https://other.example/feed.xml",
        external_id="other",
        title="Other",
        summary="S",
        link="https://other.example/other",
        author=None,
        published_at=None,
        content_hash="other-hash",
        raw_json="{}",
    )
    known_id = await repository.create_event(entry_known.id, "goal", "ghash")
    await repository.create_event(entry_other.id, "goal", "ghash")

    service = StatusService(config_manager, repository)
    views, _ = await service.list_events(
        status=None, source_id="example", q=None, limit=10, cursor=None
    )

    assert [view.event_id for view in views] == [known_id]
    assert views[0].source_id == "example"


async def test_status_service_list_events_unknown_source_url_maps_none(
    config_manager, repository
) -> None:
    entry = await repository.upsert_entry(
        source_url="https://deleted.example/feed.xml",
        external_id="orphan",
        title="Orphan",
        summary="S",
        link="https://deleted.example/orphan",
        author=None,
        published_at=None,
        content_hash="orphan-hash",
        raw_json="{}",
    )
    await repository.create_event(entry.id, "goal", "ghash")

    service = StatusService(config_manager, repository)
    views, _ = await service.list_events(status=None, source_id=None, q=None, limit=10, cursor=None)

    assert views[0].source_id is None
    assert views[0].source_url == "https://deleted.example/feed.xml"


async def test_status_service_get_event_includes_deliveries_and_truncates_goal(
    config_manager, repository
) -> None:
    long_goal = "G" * 2500
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed.xml",
        external_id="detail-control",
        title="Detail item",
        summary="Summary",
        link="https://example.com/detail-control",
        author="Alice",
        published_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        content_hash="detail-control-hash",
        raw_json="{}",
    )
    event_id = await repository.create_event(entry.id, long_goal, "goal-hash")
    delivery = await repository.create_delivery(event_id, "telegram")
    await repository.mark_delivery_success(delivery.id, "sent-ok")

    service = StatusService(config_manager, repository)
    detail = await service.get_event(event_id)

    assert detail.event.event_id == event_id
    assert detail.event.source_id == "example"
    assert detail.author == "Alice"
    assert detail.published_at == datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    assert len(detail.goal_snapshot) == 2000
    assert detail.goal_snapshot == long_goal[:2000]
    assert len(detail.deliveries) == 1
    assert detail.deliveries[0].destination_key == "telegram"
    assert detail.deliveries[0].status == "delivered"
    assert detail.deliveries[0].response_summary == "sent-ok"


async def test_status_service_get_event_missing_raises(config_manager, repository) -> None:
    service = StatusService(config_manager, repository)
    with pytest.raises(LookupError):
        await service.get_event(999999)


async def test_status_service_get_status_includes_status_counts(config_manager, repository) -> None:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed.xml",
        external_id="count-control",
        title="Count",
        summary="S",
        link="https://example.com/count-control",
        author=None,
        published_at=None,
        content_hash="count-control-hash",
        raw_json="{}",
    )
    event_id = await repository.create_event(entry.id, "goal", "ghash")
    await repository.transition_event(event_id, EventStatus.DISCOVERED, EventStatus.SCREENING)
    await repository.transition_event(
        event_id, EventStatus.SCREENING, EventStatus.FILTERED, decision_reason="nope"
    )

    status = await StatusService(config_manager, repository).get_status()
    assert status.status_counts.get(EventStatus.FILTERED.value, 0) >= 1


async def test_recovery_service_lists_and_retries_failed_events(repository) -> None:
    entry = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="failed-control",
        title="Failed",
        summary="Summary",
        link="https://example.com/failed-control",
        author=None,
        published_at=None,
        content_hash="failed-control-hash",
        raw_json="{}",
    )
    event_id = await repository.create_event(entry.id, "goal", "goal-hash")
    await repository.transition_event(event_id, EventStatus.DISCOVERED, EventStatus.SCREENING)
    for _ in range(5):
        await repository.schedule_event_retry(event_id, EventStatus.SCREENING, "AI unavailable")
        event = await repository.get_event(event_id)
        if event.status is EventStatus.RETRY_WAIT:
            await repository.make_event_due(event_id)
            await repository.resume_event(event_id)

    service = RecoveryService(repository)
    failed = await service.list_failed_events()
    assert failed[0].event_id == event_id
    assert failed[0].failed_stage == "screening"
    assert await service.retry_failed_event(event_id) is True


class FakeApprise:
    def __init__(self) -> None:
        self.calls = []

    async def notify(self, key: str, title: str, body: str) -> str:
        self.calls.append((key, title, body))
        return "sent"


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[Notification] = []

    async def notify(self, notification: Notification) -> str:
        self.calls.append(notification)
        return "telegram_message_id=1"


class FakeQQ:
    def __init__(self) -> None:
        self.destination_key = "qq:group:987"
        self.calls: list[Notification] = []

    async def notify(self, notification: Notification) -> str:
        self.calls.append(notification)
        return "qq_message_id=1"


async def test_destination_service_sends_marked_apprise_test(config_manager) -> None:
    apprise = FakeApprise()
    service = DestinationService(config_manager, apprise, None)

    result = await service.test()

    assert result == "sent"
    key, title, body = apprise.calls[0]
    assert key == "telegram"
    assert "FeedSentry TEST" in title
    assert "FeedSentry TEST" in body


async def test_destination_service_sends_marked_telegram_test(config_manager) -> None:
    telegram = FakeTelegram()
    config_manager.current = config_manager.current.model_copy(
        update={"destination": DestinationConfig(kind="telegram")}
    )
    service = DestinationService(config_manager, FakeApprise(), telegram)

    result = await service.test()

    assert result == "telegram_message_id=1"
    assert "FeedSentry TEST" in telegram.calls[0].title
    assert "FeedSentry TEST" in telegram.calls[0].summary


async def test_destination_service_sends_marked_qq_test(config_manager) -> None:
    qq = FakeQQ()
    config_manager.current = config_manager.current.model_copy(
        update={"destination": DestinationConfig(kind="qq")}
    )
    service = DestinationService(config_manager, FakeApprise(), None, qq)

    result = await service.test()

    assert result == "qq_message_id=1"
    assert "FeedSentry TEST" in qq.calls[0].title
    assert "FeedSentry TEST" in qq.calls[0].summary


async def test_destination_service_raises_when_qq_not_configured(config_manager) -> None:
    config_manager.current = config_manager.current.model_copy(
        update={"destination": DestinationConfig(kind="qq")}
    )
    service = DestinationService(config_manager, FakeApprise(), None, None)

    with pytest.raises(RuntimeError, match="qq destination is not configured"):
        await service.test()
