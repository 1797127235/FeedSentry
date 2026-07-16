from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from feedsentry.clients.feed_validation import FeedValidator, ValidatedFeed
from feedsentry.clients.rsshub import CandidateCodec, RadarMatcher, RSSHubClient
from feedsentry.config.models import (
    ConfigManager,
    DirectSourceConfig,
    RSSHubSourceConfig,
    SourceConfig,
)
from feedsentry.config.store import ConfigStore
from feedsentry.core.domain import Notification
from feedsentry.core.repository import Repository


class Polling(Protocol):
    async def poll(self, source, goal, *, rsshub, force=False) -> int: ...


class Apprise(Protocol):
    async def notify(self, key: str, title: str, body: str) -> str: ...


class Telegram(Protocol):
    async def notify(self, notification: Notification) -> str: ...


class QQ(Protocol):
    destination_key: str

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
    last_tick_at: datetime | None = None
    status_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class EventView:
    event_id: int
    entry_id: int
    status: str
    resume_stage: str | None
    title: str
    link: str
    source_url: str
    source_id: str | None
    decision_reason: str | None
    output_title: str | None
    output_summary: str | None
    failure_count: int
    last_error: str | None
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DeliveryView:
    destination_key: str
    status: str
    attempts: int
    response_summary: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventDetailView:
    event: EventView
    author: str | None
    published_at: datetime | None
    goal_snapshot: str
    deliveries: tuple[DeliveryView, ...]


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
        for entry in validated.entries:
            await self.repository.upsert_entry(**entry.as_repository_kwargs())
        now = datetime.now(UTC)
        await self.repository.mark_feed_initialized(validated.canonical_url, now)
        await self.repository.record_feed_success(
            validated.canonical_url,
            etag=validated.etag,
            last_modified=validated.last_modified,
            checked_at=now,
            next_check_at=now,
        )
        created = await self.store.add_source(source)
        return AddSourceResult(
            self._view(source, title=validated.title),
            created=created,
            baseline_initialized=created,
        )

    def _source_id(self, title: str, url: str) -> str:
        stem = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or (
            urlsplit(url).hostname or "source"
        ).replace(".", "-")
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
    def __init__(
        self,
        manager: ConfigManager,
        repository: Repository,
        last_tick_provider: Callable[[], datetime | None] | None = None,
    ) -> None:
        self.manager = manager
        self.repository = repository
        self.last_tick_provider = last_tick_provider

    async def get_status(self) -> SystemStatus:
        if self.manager.current is None:
            raise RuntimeError("configuration is not loaded")
        current = self.manager.current
        counts = await self.repository.status_counts()
        status_counts = await self.repository.status_breakdown()
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
        last_tick_at = self.last_tick_provider() if self.last_tick_provider is not None else None
        return SystemStatus(
            sources=len(current.sources),
            enabled_sources=sum(source.enabled for source in current.sources),
            pending_events=counts.pending,
            failed_events=counts.failed,
            config_error=self.manager.last_error,
            source_statuses=tuple(views),
            last_tick_at=last_tick_at,
            status_counts=status_counts,
        )

    async def list_events(
        self,
        *,
        status: str | None,
        source_id: str | None,
        q: str | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[EventView], str | None]:
        source_url: str | None = None
        if source_id is not None:
            source_url = self._source_url_for_id(source_id)
            if source_url is None:
                return [], None
        items, next_cursor = await self.repository.list_events(
            status=status,
            source_url=source_url,
            q=q,
            limit=limit,
            cursor=cursor,
        )
        url_to_id = self._source_url_to_id()
        views = [self._event_view(item, url_to_id.get(item.source_url)) for item in items]
        return views, next_cursor

    async def get_event(self, event_id: int) -> EventDetailView:
        bundle = await self.repository.get_event_bundle(event_id)
        deliveries = await self.repository.list_deliveries_for_event(event_id)
        url_to_id = self._source_url_to_id()
        source_id = url_to_id.get(bundle.entry.source_url)
        event = bundle.event
        view = EventView(
            event_id=event.id,
            entry_id=event.entry_id,
            status=event.status.value,
            resume_stage=event.resume_stage.value if event.resume_stage is not None else None,
            title=bundle.entry.title,
            link=bundle.entry.link,
            source_url=bundle.entry.source_url,
            source_id=source_id,
            decision_reason=event.decision_reason,
            output_title=event.output_title,
            output_summary=event.output_summary,
            failure_count=event.failure_count,
            last_error=event.last_error,
            next_attempt_at=event.next_attempt_at,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )
        goal = event.goal_snapshot
        if len(goal) > 2000:
            goal = goal[:2000]
        return EventDetailView(
            event=view,
            author=bundle.entry.author,
            published_at=bundle.entry.published_at,
            goal_snapshot=goal,
            deliveries=tuple(
                DeliveryView(
                    destination_key=delivery.apprise_key,
                    status=delivery.status,
                    attempts=delivery.attempts,
                    response_summary=delivery.response_summary,
                    created_at=delivery.created_at,
                    updated_at=delivery.updated_at,
                )
                for delivery in deliveries
            ),
        )

    def _source_url_to_id(self) -> dict[str, str]:
        if self.manager.current is None:
            raise RuntimeError("configuration is not loaded")
        current = self.manager.current
        return {
            source.feed_url(current.integrations.rsshub): source.id for source in current.sources
        }

    def _source_url_for_id(self, source_id: str) -> str | None:
        if self.manager.current is None:
            raise RuntimeError("configuration is not loaded")
        current = self.manager.current
        source = next((item for item in current.sources if item.id == source_id), None)
        if source is None:
            return None
        return source.feed_url(current.integrations.rsshub)

    @staticmethod
    def _event_view(item, source_id: str | None) -> EventView:
        return EventView(
            event_id=item.event_id,
            entry_id=item.entry_id,
            status=item.status.value,
            resume_stage=item.resume_stage.value if item.resume_stage is not None else None,
            title=item.title,
            link=item.link,
            source_url=item.source_url,
            source_id=source_id,
            decision_reason=item.decision_reason,
            output_title=item.output_title,
            output_summary=item.output_summary,
            failure_count=item.failure_count,
            last_error=item.last_error,
            next_attempt_at=item.next_attempt_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
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
        qq: QQ | None = None,
    ) -> None:
        self.manager = manager
        self.apprise = apprise
        self.telegram = telegram
        self.qq = qq

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
        if destination.kind == "qq":
            if self.qq is None:
                raise RuntimeError("qq destination is not configured")
            return await self.qq.notify(
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
