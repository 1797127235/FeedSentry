from __future__ import annotations

from datetime import UTC, datetime

import pytest
from test_config import VALID_CONFIG

from feedsentry.config import ConfigManager
from feedsentry.config_store import ConfigStore
from feedsentry.control import FilterService, SourceService, StatusService
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
