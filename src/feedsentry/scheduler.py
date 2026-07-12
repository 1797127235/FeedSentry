from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from feedsentry.config import ConfigManager

logger = logging.getLogger(__name__)


class Repository(Protocol):
    async def source_is_due(self, source_url: str, now: datetime) -> bool: ...

    async def list_due_event_ids(self, now: datetime, limit: int) -> list[int]: ...


class Ingestion(Protocol):
    async def poll_source(self, source_url: str, goal: str) -> int: ...


class Processor(Protocol):
    async def process_event(self, event_id: int) -> None: ...


class Scheduler:
    def __init__(
        self,
        config_manager: ConfigManager,
        repository: Repository,
        ingestion: Ingestion,
        processor: Processor,
        *,
        clock: Callable[[], datetime] | None = None,
        tick_seconds: float = 1.0,
    ) -> None:
        self.config_manager = config_manager
        self.repository = repository
        self.ingestion = ingestion
        self.processor = processor
        self.clock = clock or (lambda: datetime.now(UTC))
        self.tick_seconds = tick_seconds
        self.last_tick_at: datetime | None = None
        self._stop_event = asyncio.Event()

    async def tick(self) -> None:
        self.config_manager.reload_if_changed()
        config = self.config_manager.current
        if config is None:
            return
        now = self.clock()
        for source in config.sources:
            if not source.enabled:
                continue
            source_url = str(source.url)
            if not await self.repository.source_is_due(source_url, now):
                continue
            try:
                await self.ingestion.poll_source(source_url, config.filter.goal)
            except Exception:
                logger.exception("source poll failed", extra={"source_url": source_url})
        for event_id in await self.repository.list_due_event_ids(now, limit=20):
            await self.processor.process_event(event_id)
        self.last_tick_at = now

    async def run(self) -> None:
        while not self._stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop_event.set()
