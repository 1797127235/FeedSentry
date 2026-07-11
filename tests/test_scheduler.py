from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from feedsentry.config import DestinationConfig, MonitorConfig
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
        self.source_checks: list[tuple[str, str]] = []

    async def source_is_due(self, monitor_id: str, source_url: str, now: datetime) -> bool:
        del now
        self.source_checks.append((monitor_id, source_url))
        return True

    async def list_due_event_ids(self, now: datetime, limit: int) -> list[int]:
        del now, limit
        return [42]


class FakeIngestion:
    def __init__(self) -> None:
        self.polled: list[tuple[str, str]] = []

    async def poll_monitor_source(self, monitor: MonitorConfig, source_url: str) -> int:
        self.polled.append((monitor.id, source_url))
        return 0


class FakeProcessor:
    def __init__(self) -> None:
        self.processed: list[int] = []

    async def process_event(self, event_id: int) -> None:
        self.processed.append(event_id)


def make_monitor() -> MonitorConfig:
    return MonitorConfig(
        id="monitor-a",
        name="Monitor",
        goal="Important releases",
        interval="10m",
        sources=["https://example.com/feed"],
        destination=DestinationConfig(apprise_key="telegram"),
    )


async def test_tick_reloads_polls_due_sources_and_processes_events() -> None:
    config = FakeConfig(current=type("Config", (), {"monitors": [make_monitor()]})())
    repository = FakeRepository()
    ingestion = FakeIngestion()
    processor = FakeProcessor()
    scheduler = Scheduler(
        config, repository, ingestion, processor, clock=lambda: datetime.now(UTC), tick_seconds=1
    )

    await scheduler.tick()

    assert config.reload_calls == 1
    assert ingestion.polled == [("monitor-a", "https://example.com/feed")]
    assert processor.processed == [42]
    assert scheduler.last_tick_at is not None


async def test_run_stops_cleanly() -> None:
    config = FakeConfig(current=type("Config", (), {"monitors": []})())
    scheduler = Scheduler(
        config, FakeRepository(), FakeIngestion(), FakeProcessor(), tick_seconds=60
    )

    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0)
    await scheduler.stop()
    await task
