from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from feedsentry.config.models import DirectSourceConfig
from feedsentry.pipeline.scheduler import Scheduler


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


class FakePolling:
    def __init__(self) -> None:
        self.polled: list[tuple[str, str]] = []

    async def poll(self, source, goal: str, *, rsshub) -> int:
        del rsshub
        if source.enabled:
            self.polled.append((source.feed_url(None), goal))
        return 0


class FakeProcessor:
    def __init__(self) -> None:
        self.processed: list[int] = []

    async def process_event(self, event_id: int) -> None:
        self.processed.append(event_id)


async def test_tick_polls_enabled_sources_with_global_goal_and_processes_events() -> None:
    sources = [
        DirectSourceConfig(id="feed", kind="feed", url="https://example.com/feed"),
        DirectSourceConfig(id="off", kind="feed", url="https://example.com/off", enabled=False),
    ]
    current = type(
        "Config",
        (),
        {
            "sources": sources,
            "filter": type("F", (), {"goal": "AI"})(),
            "integrations": type("I", (), {"rsshub": None})(),
        },
    )()
    config = FakeConfig(current=current)
    repository = FakeRepository()
    polling = FakePolling()
    processor = FakeProcessor()
    scheduler = Scheduler(config, repository, polling, processor, clock=lambda: datetime.now(UTC))

    await scheduler.tick()

    assert config.reload_calls == 1
    assert polling.polled == [("https://example.com/feed", "AI")]
    assert processor.processed == [42]


async def test_run_stops_cleanly() -> None:
    current = type(
        "Config",
        (),
        {
            "sources": [],
            "filter": type("F", (), {"goal": "AI"})(),
            "integrations": type("I", (), {"rsshub": None})(),
        },
    )()
    scheduler = Scheduler(FakeConfig(current), FakeRepository(), FakePolling(), FakeProcessor())
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0)
    await scheduler.stop()
    await task
    assert scheduler.is_running is False


async def test_tick_continues_after_one_event_fails() -> None:
    current = type(
        "Config",
        (),
        {
            "sources": [],
            "filter": type("F", (), {"goal": "AI"})(),
            "integrations": type("I", (), {"rsshub": None})(),
        },
    )()
    repository = FakeRepository()
    repository.list_due_event_ids = lambda now, limit: _event_ids(now, limit)
    processor = FakeProcessor()

    async def process(event_id: int) -> None:
        if event_id == 1:
            raise RuntimeError("broken event")
        processor.processed.append(event_id)

    processor.process_event = process
    scheduler = Scheduler(FakeConfig(current), repository, FakePolling(), processor)

    await scheduler.tick()

    assert processor.processed == [2]


async def _event_ids(now: datetime, limit: int) -> list[int]:
    del now, limit
    return [1, 2]


async def test_run_survives_a_tick_exception() -> None:
    current = type(
        "Config",
        (),
        {
            "sources": [],
            "filter": type("F", (), {"goal": "AI"})(),
            "integrations": type("I", (), {"rsshub": None})(),
        },
    )()
    scheduler = Scheduler(
        FakeConfig(current), FakeRepository(), FakePolling(), FakeProcessor(), tick_seconds=0.01
    )
    calls = 0

    async def tick() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient database failure")
        await scheduler.stop()

    scheduler.tick = tick
    await asyncio.wait_for(scheduler.run(), timeout=1)

    assert calls == 2
    assert scheduler.is_running is False


async def test_tick_processes_events_with_bounded_concurrency() -> None:
    current = type(
        "Config",
        (),
        {
            "sources": [],
            "filter": type("F", (), {"goal": "AI"})(),
            "integrations": type("I", (), {"rsshub": None})(),
        },
    )()
    repository = FakeRepository()
    repository.list_due_event_ids = lambda now, limit: _three_event_ids(now, limit)
    active = 0
    max_active = 0
    release = asyncio.Event()
    started = asyncio.Event()

    async def process(event_id: int) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if max_active == 2:
            started.set()
        await release.wait()
        active -= 1

    processor = FakeProcessor()
    processor.process_event = process
    scheduler = Scheduler(
        FakeConfig(current),
        repository,
        FakePolling(),
        processor,
        event_concurrency=2,
    )
    task = asyncio.create_task(scheduler.tick())

    await asyncio.wait_for(started.wait(), timeout=1)
    assert max_active == 2
    release.set()
    await task


async def _three_event_ids(now: datetime, limit: int) -> list[int]:
    del now, limit
    return [1, 2, 3]
