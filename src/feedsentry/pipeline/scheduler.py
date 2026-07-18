from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from feedsentry.config.models import ConfigManager

logger = logging.getLogger(__name__)


class Repository(Protocol):
    async def list_due_event_ids(self, now: datetime, limit: int) -> list[int]: ...


class Polling(Protocol):
    async def poll(self, source: object, goal: str, *, rsshub: object | None) -> int: ...


class Processor(Protocol):
    async def process_event(self, event_id: int) -> None: ...


class Scheduler:
    def __init__(
        self,
        config_manager: ConfigManager,
        repository: Repository,
        polling: Polling,
        processor: Processor,
        *,
        clock: Callable[[], datetime] | None = None,
        tick_seconds: float = 1.0,
        source_concurrency: int = 4,
        event_concurrency: int = 4,
    ) -> None:
        self.config_manager = config_manager
        self.repository = repository
        self.polling = polling
        self.processor = processor
        self.clock = clock or (lambda: datetime.now(UTC))
        self.tick_seconds = tick_seconds
        self.source_concurrency = max(1, source_concurrency)
        self.event_concurrency = max(1, event_concurrency)
        self.last_tick_at: datetime | None = None
        self.is_running = False
        self._stop_event = asyncio.Event()

    async def tick(self) -> None:
        self.config_manager.reload_if_changed()
        config = self.config_manager.current
        if config is None:
            return
        now = self.clock()

        async def poll_source(source: object) -> None:
            try:
                await self.polling.poll(
                    source,
                    config.filter.goal,
                    rsshub=config.integrations.rsshub,
                )
            except Exception:
                logger.exception("source poll failed", extra={"source_id": source.id})

        for offset in range(0, len(config.sources), self.source_concurrency):
            batch = config.sources[offset : offset + self.source_concurrency]
            await asyncio.gather(*(poll_source(source) for source in batch))

        async def process_event(event_id: int) -> None:
            try:
                await self.processor.process_event(event_id)
            except Exception:
                logger.exception("event processing failed", extra={"event_id": event_id})

        event_ids = await self.repository.list_due_event_ids(now, limit=20)
        for offset in range(0, len(event_ids), self.event_concurrency):
            batch = event_ids[offset : offset + self.event_concurrency]
            await asyncio.gather(*(process_event(event_id) for event_id in batch))
        self.last_tick_at = now

    async def run(self) -> None:
        self.is_running = True
        try:
            while not self._stop_event.is_set():
                try:
                    await self.tick()
                except Exception:
                    logger.exception("scheduler tick failed")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.tick_seconds)
                except TimeoutError:
                    pass
        finally:
            self.is_running = False

    async def stop(self) -> None:
        self._stop_event.set()
