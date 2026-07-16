from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from feedsentry.config.models import RSSHubConfig, SourceConfig


class Repository(Protocol):
    async def source_is_due(self, source_url: str, now: datetime) -> bool: ...


class Ingestion(Protocol):
    async def poll_source(self, source_url: str, goal: str) -> int: ...


class PollCoordinator:
    def __init__(
        self,
        repository: Repository,
        ingestion: Ingestion,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.ingestion = ingestion
        self.clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def poll(
        self,
        source: SourceConfig,
        goal: str,
        *,
        rsshub: RSSHubConfig | None,
        force: bool = False,
    ) -> int:
        if not source.enabled and not force:
            return 0
        source_url = source.feed_url(rsshub)
        lock = await self._lock_for(source.id)
        async with lock:
            if not force and not await self.repository.source_is_due(source_url, self.clock()):
                return 0
            return await self.ingestion.poll_source(source_url, goal)

    async def _lock_for(self, source_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(source_id, asyncio.Lock())
