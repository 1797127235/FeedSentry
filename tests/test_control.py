from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_config import VALID_CONFIG

from feedsentry.config import ConfigManager, DestinationConfig
from feedsentry.config_store import ConfigStore
from feedsentry.control import (
    DestinationService,
    FilterService,
    RecoveryService,
    SourceService,
    StatusService,
)
from feedsentry.domain import EventStatus, Notification
from feedsentry.feed_validation import ValidatedFeed
from feedsentry.feeds import NormalizedEntry
from feedsentry.rsshub import CandidateCodec


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


async def test_add_direct_feed_is_idempotent(source_service) -> None:
    first = await source_service.add_feed("https://news.example/feed.xml")
    second = await source_service.add_feed("https://news.example/feed.xml")
    assert first.source.id == second.source.id
    assert second.created is False


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


async def test_filter_service_reads_and_updates_goal(config_manager) -> None:
    service = FilterService(config_manager, ConfigStore(config_manager))
    assert service.get_goal() == "Important releases only"
    assert await service.set_goal("Security releases only") is True
    assert service.get_goal() == "Security releases only"


async def test_status_service_returns_source_health(config_manager, repository) -> None:
    await repository.record_feed_failure(
        "https://example.com/feed.xml",
        error="timeout",
        checked_at=datetime.now(UTC),
        next_check_at=datetime.now(UTC),
    )
    status = await StatusService(config_manager, repository).get_status()

    assert status.sources == 1
    assert status.enabled_sources == 1
    assert status.source_statuses[0].consecutive_failures == 1
    assert status.source_statuses[0].last_error == "timeout"


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
