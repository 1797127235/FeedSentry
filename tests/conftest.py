from __future__ import annotations

from collections.abc import Callable

import pytest

from feedsentry.config import DestinationConfig, MonitorConfig
from feedsentry.database import create_database
from feedsentry.feeds import FeedFetchResult
from feedsentry.repository import Repository


class FakeAIClient:
    def __init__(self) -> None:
        self.screen_result = None
        self.summary_result = None
        self.screen_calls = 0
        self.summary_calls = 0

    async def screen(self, goal: str, title: str, feed_summary: str):
        del goal, title, feed_summary
        self.screen_calls += 1
        if self.screen_result is None:
            raise RuntimeError("fake screen result was not configured")
        return self.screen_result

    async def summarize(self, goal: str, title: str, markdown: str):
        del goal, title, markdown
        self.summary_calls += 1
        if self.summary_result is None:
            raise RuntimeError("fake summary result was not configured")
        return self.summary_result


class FakeFirecrawlClient:
    def __init__(self) -> None:
        self.markdown: str | None = None
        self.error: Exception | None = None
        self.calls = 0

    async def scrape(self, url: str) -> str:
        del url
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.markdown is None:
            raise RuntimeError("fake markdown was not configured")
        return self.markdown


class FakeAppriseClient:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls = 0
        self.notifications: list[tuple[str, str, str]] = []

    async def notify(self, key: str, title: str, body: str) -> str:
        self.calls += 1
        self.notifications.append((key, title, body))
        if self.error is not None:
            raise self.error
        return "sent"


class FakeFeedClient:
    def __init__(self) -> None:
        self.result: FeedFetchResult | None = None
        self.error: Exception | None = None
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def fetch(
        self, source_url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FeedFetchResult:
        self.calls.append((source_url, etag, last_modified))
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise RuntimeError("fake feed result was not configured")
        return self.result


@pytest.fixture
async def database(tmp_path):
    instance = create_database(tmp_path / "feedsentry.db")
    await instance.initialize()
    yield instance
    await instance.dispose()


@pytest.fixture
async def repository(database) -> Repository:
    return Repository(database.session_factory)


@pytest.fixture
def fake_feed_client() -> FakeFeedClient:
    return FakeFeedClient()


@pytest.fixture
def make_monitor() -> Callable[[], MonitorConfig]:
    def factory() -> MonitorConfig:
        return MonitorConfig(
            id="monitor-a",
            name="Example monitor",
            goal="Important releases",
            interval="10m",
            sources=["https://example.com/feed"],
            destination=DestinationConfig(apprise_key="telegram"),
        )

    return factory
