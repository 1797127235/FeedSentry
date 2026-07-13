from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from feedsentry.config import ConfigManager

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
    ) -> None:
        self.config_manager = config_manager
        self.repository = repository
        self.polling = polling
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
            try:
                await self.polling.poll(
                    source,
                    config.filter.goal,
                    rsshub=config.integrations.rsshub,
                )
            except Exception:
                logger.exception("source poll failed", extra={"source_id": source.id})
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
