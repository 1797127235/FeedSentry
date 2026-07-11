from __future__ import annotations

from collections.abc import Callable

import pytest

from feedsentry.config import DestinationConfig, MonitorConfig
from feedsentry.database import create_database
from feedsentry.feeds import FeedFetchResult
from feedsentry.repository import Repository


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
