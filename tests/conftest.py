from __future__ import annotations

import pytest

from feedsentry.clients.feeds import FeedFetchResult
from feedsentry.core.database import create_database
from feedsentry.core.repository import Repository

VALID_CONFIG = """
integrations:
  firecrawl:
    base_url: ${FIRECRAWL_URL}
    api_key: ${FIRECRAWL_KEY:-}
  apprise:
    base_url: http://apprise:8000
  rsshub:
    base_url: https://rsshub.antest.cc.cd
ai:
  base_url: http://llm:8080/v1
  api_key: secret-ai-key
  model: test-model
storage:
  path: ./data/test.db
filter:
  goal: Important releases only
sources:
  - id: example
    kind: feed
    url: https://example.com/feed.xml
    enabled: true
destination:
  apprise_key: telegram
"""


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
