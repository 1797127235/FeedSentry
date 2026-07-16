from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from feedsentry.config.models import DirectSourceConfig
from feedsentry.pipeline.polling import PollCoordinator


class FakeRepository:
    def __init__(self) -> None:
        self.due = True

    async def source_is_due(self, source_url: str, now: datetime) -> bool:
        del source_url, now
        return self.due


class BlockingIngestion:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    async def poll_source(self, source_url: str, goal: str) -> int:
        del goal
        self.calls.append(source_url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return 0


async def test_same_source_polls_do_not_overlap() -> None:
    ingestion = BlockingIngestion()
    coordinator = PollCoordinator(FakeRepository(), ingestion)
    source = DirectSourceConfig(id="one", kind="feed", url="https://example.com/feed")

    await asyncio.gather(
        coordinator.poll(source, "goal", rsshub=None, force=True),
        coordinator.poll(source, "goal", rsshub=None, force=True),
    )

    assert ingestion.max_active == 1
    assert len(ingestion.calls) == 2


async def test_different_sources_can_poll_concurrently() -> None:
    ingestion = BlockingIngestion()
    coordinator = PollCoordinator(FakeRepository(), ingestion)
    first = DirectSourceConfig(id="one", kind="feed", url="https://example.com/one")
    second = DirectSourceConfig(id="two", kind="feed", url="https://example.com/two")

    await asyncio.gather(
        coordinator.poll(first, "goal", rsshub=None, force=True),
        coordinator.poll(second, "goal", rsshub=None, force=True),
    )

    assert ingestion.max_active == 2


async def test_normal_poll_respects_enabled_and_due_state() -> None:
    repository = FakeRepository()
    ingestion = BlockingIngestion()
    coordinator = PollCoordinator(repository, ingestion, clock=lambda: datetime.now(UTC))
    disabled = DirectSourceConfig(
        id="off", kind="feed", url="https://example.com/off", enabled=False
    )
    enabled = DirectSourceConfig(id="on", kind="feed", url="https://example.com/on")

    assert await coordinator.poll(disabled, "goal", rsshub=None) == 0
    repository.due = False
    assert await coordinator.poll(enabled, "goal", rsshub=None) == 0
    assert ingestion.calls == []
    assert await coordinator.poll(enabled, "goal", rsshub=None, force=True) == 0
    assert ingestion.calls == ["https://example.com/on"]
