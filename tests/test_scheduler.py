from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from feedsentry.scheduler import Scheduler


@dataclass
class FakeConfig:
    current: object
    reload_calls: int = 0

    def reload_if_changed(self) -> bool:
        self.reload_calls += 1
        return False


class FakeRepository:
    def __init__(self) -> None:
        self.source_checks: list[str] = []

    async def source_is_due(self, source_url: str, now: datetime) -> bool:
        del now
        self.source_checks.append(source_url)
        return True

    async def list_due_event_ids(self, now: datetime, limit: int) -> list[int]:
        del now, limit
        return [42]


class FakeIngestion:
    def __init__(self) -> None:
        self.polled: list[tuple[str, str]] = []

    async def poll_source(self, source_url: str, goal: str) -> int:
        self.polled.append((source_url, goal))
        return 0


class FakeProcessor:
    def __init__(self) -> None:
        self.processed: list[int] = []

    async def process_event(self, event_id: int) -> None:
        self.processed.append(event_id)


async def test_tick_polls_enabled_sources_with_global_goal_and_processes_events() -> None:
    sources = [
        type("Source", (), {"url": "https://example.com/feed", "enabled": True})(),
        type("Source", (), {"url": "https://example.com/off", "enabled": False})(),
    ]
    current = type("Config", (), {"sources": sources, "filter": type("F", (), {"goal": "AI"})()})()
    config = FakeConfig(current=current)
    repository = FakeRepository()
    ingestion = FakeIngestion()
    processor = FakeProcessor()
    scheduler = Scheduler(config, repository, ingestion, processor, clock=lambda: datetime.now(UTC))

    await scheduler.tick()

    assert config.reload_calls == 1
    assert ingestion.polled == [("https://example.com/feed", "AI")]
    assert processor.processed == [42]


async def test_run_stops_cleanly() -> None:
    current = type("Config", (), {"sources": [], "filter": type("F", (), {"goal": "AI"})()})()
    scheduler = Scheduler(FakeConfig(current), FakeRepository(), FakeIngestion(), FakeProcessor())
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0)
    await scheduler.stop()
    await task
