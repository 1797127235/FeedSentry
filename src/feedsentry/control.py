from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from feedsentry.config import (
    ConfigManager,
    DirectSourceConfig,
    RSSHubSourceConfig,
    SourceConfig,
)
from feedsentry.config_store import ConfigStore
from feedsentry.domain import Notification
from feedsentry.feed_validation import FeedValidator, ValidatedFeed
from feedsentry.repository import Repository
from feedsentry.rsshub import CandidateCodec, RadarMatcher, RSSHubClient


class Polling(Protocol):
    async def poll(self, source, goal, *, rsshub, force=False) -> int: ...


class Apprise(Protocol):
    async def notify(self, key: str, title: str, body: str) -> str: ...


class Telegram(Protocol):
    async def notify(self, notification: Notification) -> str: ...


@dataclass(frozen=True)
class SourceView:
    id: str
    kind: str
    feed_url: str
    enabled: bool
    title: str | None = None
    page_url: str | None = None
    route: str | None = None
    initialized_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    next_check_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class AddSourceResult:
    source: SourceView
    created: bool
    baseline_initialized: bool


@dataclass(frozen=True)
class CandidateView:
    candidate_id: str
    title: str
    page_url: str
    feed_url: str


@dataclass(frozen=True)
class SystemStatus:
    sources: int
    enabled_sources: int
    pending_events: int
    failed_events: int
    config_error: str | None
    source_statuses: tuple[SourceView, ...]


@dataclass(frozen=True)
class FailedEventView:
    event_id: int
    entry_id: int
    title: str
    failed_stage: str
    failure_count: int
    last_error: str | None
    updated_at: datetime


class SourceService:
    def __init__(
        self,
        manager: ConfigManager,
        store: ConfigStore,
        repository: Repository,
        validator: FeedValidator,
        rsshub: RSSHubClient,
        candidates: CandidateCodec,
        polling: Polling,
    ) -> None:
        self.manager = manager
        self.store = store
        self.repository = repository
        self.validator = validator
        self.rsshub = rsshub
        self.candidates = candidates
        self.polling = polling

    async def discover_feeds(self, page_url: str) -> list[CandidateView]:
        current = self._current()
        if current.integrations.rsshub is None:
            raise RuntimeError("RSSHub is not configured")
        discovered = RadarMatcher().discover(
            page_url,
            await self.rsshub.rules(),
            str(current.integrations.rsshub.base_url),
        )
        return [
            CandidateView(
                candidate_id=self.candidates.encode(candidate),
                title=candidate.title,
                page_url=candidate.page_url,
                feed_url=candidate.feed_url,
            )
            for candidate in discovered
        ]

    async def subscribe_feed(self, candidate_id: str) -> AddSourceResult:
        candidate = self.candidates.decode(candidate_id)
        validated = await self.validator.validate(candidate.feed_url)
        source = RSSHubSourceConfig(
            id=self._source_id(validated.title, validated.canonical_url),
            kind="rsshub",
            page_url=candidate.page_url,
            route=candidate.route,
        )
        return await self._add_validated(source, validated)

    async def add_feed(self, url: str) -> AddSourceResult:
        validated = await self.validator.validate(url)
        existing = self._source_by_url(validated.canonical_url)
        if existing is not None:
            return AddSourceResult(
                self._view(existing, title=validated.title),
                created=False,
                baseline_initialized=await self.repository.feed_is_initialized(
                    validated.canonical_url
                ),
            )
        source = DirectSourceConfig(
            id=self._source_id(validated.title, validated.canonical_url),
            kind="feed",
            url=validated.canonical_url,
        )
        return await self._add_validated(source, validated)

    async def list_sources(self) -> list[SourceView]:
        states = {state.source_url: state for state in await self.repository.list_feed_states()}
        return [
            self._view(source, state=states.get(self._feed_url(source)))
            for source in self._current().sources
        ]

    async def set_enabled(self, source_id: str, enabled: bool) -> bool:
        return await self.store.set_source_enabled(source_id, enabled)

    async def remove(self, source_id: str) -> bool:
        return await self.store.remove_source(source_id)

    async def check_now(self, source_id: str) -> int:
        source = self._source(source_id)
        current = self._current()
        return await self.polling.poll(
            source,
            current.filter.goal,
            rsshub=current.integrations.rsshub,
            force=True,
        )

    async def _add_validated(
        self, source: SourceConfig, validated: ValidatedFeed
    ) -> AddSourceResult:
        existing = self._source_by_url(validated.canonical_url)
        if existing is not None:
            return AddSourceResult(
                self._view(existing, title=validated.title),
                created=False,
                baseline_initialized=await self.repository.feed_is_initialized(
                    validated.canonical_url
                ),
            )
        created = await self.store.add_source(source)
        baseline_initialized = False
        if created:
            now = datetime.now(UTC)
            for entry in validated.entries:
                await self.repository.upsert_entry(**entry.as_repository_kwargs())
            await self.repository.mark_feed_initialized(validated.canonical_url, now)
            await self.repository.record_feed_success(
                validated.canonical_url,
                etag=validated.etag,
                last_modified=validated.last_modified,
                checked_at=now,
                next_check_at=now,
            )
            baseline_initialized = True
        return AddSourceResult(
            self._view(source, title=validated.title),
            created=created,
            baseline_initialized=baseline_initialized,
        )

    def _source_id(self, title: str, url: str) -> str:
        stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or (
            urlsplit(url).hostname or "source"
        ).replace(".", "-")
        existing = {source.id for source in self._current().sources}
        if stem not in existing:
            return stem[:64]
        suffix = hashlib.sha256(url.encode()).hexdigest()[:8]
        return f"{stem[:55]}-{suffix}"

    def _source_by_url(self, url: str) -> SourceConfig | None:
        return next(
            (source for source in self._current().sources if self._feed_url(source) == url), None
        )

    def _source(self, source_id: str) -> SourceConfig:
        source = next(
            (source for source in self._current().sources if source.id == source_id), None
        )
        if source is None:
            raise LookupError(f"source not found: {source_id}")
        return source

    def _feed_url(self, source: SourceConfig) -> str:
        return source.feed_url(self._current().integrations.rsshub)

    def _view(self, source: SourceConfig, *, title: str | None = None, state=None) -> SourceView:
        return SourceView(
            id=source.id,
            kind=source.kind,
            feed_url=self._feed_url(source),
            enabled=source.enabled,
            title=title,
            page_url=str(source.page_url) if source.kind == "rsshub" else None,
            route=source.route if source.kind == "rsshub" else None,
            initialized_at=state.initialized_at if state else None,
            last_success_at=state.last_success_at if state else None,
            consecutive_failures=state.consecutive_failures if state else 0,
            next_check_at=state.next_check_at if state else None,
            last_error=state.last_error if state else None,
        )

    def _current(self):
        if self.manager.current is None:
            raise RuntimeError("configuration is not loaded")
        return self.manager.current


class FilterService:
    def __init__(self, manager: ConfigManager, store: ConfigStore) -> None:
        self.manager = manager
        self.store = store

    def get_goal(self) -> str:
        if self.manager.current is None:
            raise RuntimeError("configuration is not loaded")
        return self.manager.current.filter.goal

    async def set_goal(self, goal: str) -> bool:
        return await self.store.set_filter_goal(goal)


class StatusService:
    def __init__(self, manager: ConfigManager, repository: Repository) -> None:
        self.manager = manager
        self.repository = repository

    async def get_status(self) -> SystemStatus:
        if self.manager.current is None:
            raise RuntimeError("configuration is not loaded")
        current = self.manager.current
        counts = await self.repository.status_counts()
        states = {state.source_url: state for state in await self.repository.list_feed_states()}
        views = []
        for source in current.sources:
            url = source.feed_url(current.integrations.rsshub)
            state = states.get(url)
            views.append(
                SourceView(
                    id=source.id,
                    kind=source.kind,
                    feed_url=url,
                    enabled=source.enabled,
                    initialized_at=state.initialized_at if state else None,
                    last_success_at=state.last_success_at if state else None,
                    consecutive_failures=state.consecutive_failures if state else 0,
                    next_check_at=state.next_check_at if state else None,
                    last_error=state.last_error if state else None,
                )
            )
        return SystemStatus(
            sources=len(current.sources),
            enabled_sources=sum(source.enabled for source in current.sources),
            pending_events=counts.pending,
            failed_events=counts.failed,
            config_error=self.manager.last_error,
            source_statuses=tuple(views),
        )


class RecoveryService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def list_failed_events(self) -> list[FailedEventView]:
        records = await self.repository.list_failed_events()
        return [
            FailedEventView(
                event_id=record.event_id,
                entry_id=record.entry_id,
                title=record.title,
                failed_stage=record.failed_stage.value,
                failure_count=record.failure_count,
                last_error=record.last_error,
                updated_at=record.updated_at,
            )
            for record in records
        ]

    async def retry_failed_event(self, event_id: int) -> bool:
        return await self.repository.retry_failed_event(event_id)


class DestinationService:
    def __init__(
        self,
        manager: ConfigManager,
        apprise: Apprise,
        telegram: Telegram | None,
    ) -> None:
        self.manager = manager
        self.apprise = apprise
        self.telegram = telegram

    async def test(self) -> str:
        if self.manager.current is None:
            raise RuntimeError("configuration is not loaded")
        destination = self.manager.current.destination
        title = "FeedSentry TEST notification"
        body = "FeedSentry TEST: notification delivery is working."
        if destination.kind == "telegram":
            if self.telegram is None:
                raise RuntimeError("telegram destination is not configured")
            return await self.telegram.notify(
                Notification(
                    title=title,
                    summary=body,
                    source_url="https://feedsentry.invalid/test",
                    link="https://feedsentry.invalid/test",
                )
            )
        if destination.apprise_key is None:
            raise RuntimeError("apprise destination is not configured")
        return await self.apprise.notify(destination.apprise_key, title, body)
