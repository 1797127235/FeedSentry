from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedsentry.database import DeliveryRow, EntryRow, EventRow, FeedStateRow, ScrapeCacheRow
from feedsentry.domain import EventStatus, assert_transition, next_retry_at


@dataclass(frozen=True)
class EntryRecord:
    id: int
    source_url: str
    external_id: str
    title: str
    summary: str
    link: str
    author: str | None
    published_at: datetime | None
    content_hash: str
    raw_json: str
    first_seen_at: datetime


@dataclass(frozen=True)
class FeedStateRecord:
    source_url: str
    etag: str | None
    last_modified: str | None
    initialized_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    next_check_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class EventRecord:
    id: int
    entry_id: int
    status: EventStatus
    resume_stage: EventStatus | None
    goal_snapshot: str
    goal_hash: str
    decision_reason: str | None
    output_title: str | None
    output_summary: str | None
    failure_count: int
    last_error: str | None
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ScrapeRecord:
    url: str
    markdown: str
    content_hash: str
    fetched_at: datetime


@dataclass(frozen=True)
class DeliveryRecord:
    id: int
    event_id: int
    apprise_key: str
    idempotency_key: str
    status: str
    attempts: int
    response_summary: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventBundle:
    event: EventRecord
    entry: EntryRecord
    scrape: ScrapeRecord | None


@dataclass(frozen=True)
class StatusCounts:
    pending: int
    failed: int


class Repository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ping(self) -> bool:
        async with self._session_factory() as session:
            await session.scalar(select(1))
        return True

    async def status_counts(self) -> StatusCounts:
        async with self._session_factory() as session:
            pending = await session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(
                    EventRow.status.not_in(
                        (
                            EventStatus.FILTERED.value,
                            EventStatus.DELIVERED.value,
                            EventStatus.FAILED.value,
                        )
                    )
                )
            )
            failed = await session.scalar(
                select(func.count())
                .select_from(EventRow)
                .where(EventRow.status == EventStatus.FAILED.value)
            )
        return StatusCounts(pending=int(pending or 0), failed=int(failed or 0))

    async def feed_is_initialized(self, source_url: str) -> bool:
        async with self._session_factory() as session:
            result = await session.scalar(
                select(FeedStateRow.initialized_at).where(
                    FeedStateRow.source_url == source_url,
                )
            )
        return result is not None

    async def get_feed_state(self, source_url: str) -> FeedStateRecord | None:
        async with self._session_factory() as session:
            row = await session.get(FeedStateRow, source_url)
        if row is None:
            return None
        return FeedStateRecord(
            source_url=row.source_url,
            etag=row.etag,
            last_modified=row.last_modified,
            initialized_at=row.initialized_at,
            last_success_at=row.last_success_at,
            consecutive_failures=row.consecutive_failures,
            next_check_at=row.next_check_at,
            last_error=row.last_error,
        )

    async def list_feed_states(self) -> list[FeedStateRecord]:
        async with self._session_factory() as session:
            rows = await session.scalars(select(FeedStateRow).order_by(FeedStateRow.source_url))
            return [
                FeedStateRecord(
                    source_url=row.source_url,
                    etag=row.etag,
                    last_modified=row.last_modified,
                    initialized_at=row.initialized_at,
                    last_success_at=row.last_success_at,
                    consecutive_failures=row.consecutive_failures,
                    next_check_at=row.next_check_at,
                    last_error=row.last_error,
                )
                for row in rows
            ]

    async def record_feed_success(
        self,
        source_url: str,
        *,
        etag: str | None,
        last_modified: str | None,
        checked_at: datetime,
        next_check_at: datetime,
    ) -> None:
        statement = insert(FeedStateRow).values(
            source_url=source_url,
            etag=etag,
            last_modified=last_modified,
            last_success_at=checked_at,
            consecutive_failures=0,
            next_check_at=next_check_at,
            last_error=None,
        )
        statement = statement.on_conflict_do_update(
            index_elements=("source_url",),
            set_={
                "etag": statement.excluded.etag,
                "last_modified": statement.excluded.last_modified,
                "last_success_at": statement.excluded.last_success_at,
                "consecutive_failures": 0,
                "next_check_at": statement.excluded.next_check_at,
                "last_error": None,
            },
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def record_feed_failure(
        self,
        source_url: str,
        *,
        error: str,
        checked_at: datetime,
        next_check_at: datetime,
    ) -> None:
        del checked_at
        statement = insert(FeedStateRow).values(
            source_url=source_url,
            consecutive_failures=1,
            next_check_at=next_check_at,
            last_error=error,
        )
        statement = statement.on_conflict_do_update(
            index_elements=("source_url",),
            set_={
                "consecutive_failures": FeedStateRow.consecutive_failures + 1,
                "next_check_at": statement.excluded.next_check_at,
                "last_error": statement.excluded.last_error,
            },
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def source_is_due(self, source_url: str, now: datetime) -> bool:
        state = await self.get_feed_state(source_url)
        return state is None or state.next_check_at is None or state.next_check_at <= now

    async def mark_feed_initialized(self, source_url: str, initialized_at: datetime) -> None:
        statement = (
            insert(FeedStateRow)
            .values(
                source_url=source_url,
                initialized_at=initialized_at,
                consecutive_failures=0,
            )
            .on_conflict_do_nothing(index_elements=("source_url",))
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def upsert_entry(
        self,
        *,
        source_url: str,
        external_id: str,
        title: str,
        summary: str,
        link: str,
        author: str | None,
        published_at: datetime | None,
        content_hash: str,
        raw_json: str,
    ) -> EntryRecord:
        statement = (
            insert(EntryRow)
            .values(
                source_url=source_url,
                external_id=external_id,
                title=title,
                summary=summary,
                link=link,
                author=author,
                published_at=published_at,
                content_hash=content_hash,
                raw_json=raw_json,
                first_seen_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=("source_url", "external_id"))
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)
            row = await session.scalar(
                select(EntryRow).where(
                    EntryRow.source_url == source_url,
                    EntryRow.external_id == external_id,
                )
            )
        if row is None:
            raise RuntimeError("entry insert did not produce a row")
        return EntryRecord(
            id=row.id,
            source_url=row.source_url,
            external_id=row.external_id,
            title=row.title,
            summary=row.summary,
            link=row.link,
            author=row.author,
            published_at=row.published_at,
            content_hash=row.content_hash,
            raw_json=row.raw_json,
            first_seen_at=row.first_seen_at,
        )

    async def count_events(self) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(EventRow))
        return int(count or 0)

    async def create_event(self, entry_id: int, goal: str, goal_digest: str) -> int:
        now = datetime.now(UTC)
        statement = (
            insert(EventRow)
            .values(
                entry_id=entry_id,
                status=EventStatus.DISCOVERED.value,
                goal_snapshot=goal,
                goal_hash=goal_digest,
                failure_count=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=("entry_id",))
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)
            event_id = await session.scalar(
                select(EventRow.id).where(EventRow.entry_id == entry_id)
            )
        if event_id is None:
            raise RuntimeError("event insert did not produce an id")
        return event_id

    async def get_event(self, event_id: int) -> EventRecord:
        async with self._session_factory() as session:
            row = await session.get(EventRow, event_id)
        if row is None:
            raise LookupError(f"event not found: {event_id}")
        return self._event_record(row)

    @staticmethod
    def _event_record(row: EventRow) -> EventRecord:
        return EventRecord(
            id=row.id,
            entry_id=row.entry_id,
            status=EventStatus(row.status),
            resume_stage=EventStatus(row.resume_stage) if row.resume_stage is not None else None,
            goal_snapshot=row.goal_snapshot,
            goal_hash=row.goal_hash,
            decision_reason=row.decision_reason,
            output_title=row.output_title,
            output_summary=row.output_summary,
            failure_count=row.failure_count,
            last_error=row.last_error,
            next_attempt_at=row.next_attempt_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _entry_record(row: EntryRow) -> EntryRecord:
        return EntryRecord(
            id=row.id,
            source_url=row.source_url,
            external_id=row.external_id,
            title=row.title,
            summary=row.summary,
            link=row.link,
            author=row.author,
            published_at=row.published_at,
            content_hash=row.content_hash,
            raw_json=row.raw_json,
            first_seen_at=row.first_seen_at,
        )

    @staticmethod
    def _scrape_record(row: ScrapeCacheRow) -> ScrapeRecord:
        return ScrapeRecord(
            url=row.url,
            markdown=row.markdown,
            content_hash=row.content_hash,
            fetched_at=row.fetched_at,
        )

    @staticmethod
    def _delivery_record(row: DeliveryRow) -> DeliveryRecord:
        return DeliveryRecord(
            id=row.id,
            event_id=row.event_id,
            apprise_key=row.apprise_key,
            idempotency_key=row.idempotency_key,
            status=row.status,
            attempts=row.attempts,
            response_summary=row.response_summary,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def get_event_bundle(self, event_id: int) -> EventBundle:
        async with self._session_factory() as session:
            row = await session.execute(
                select(EventRow, EntryRow, ScrapeCacheRow)
                .join(EntryRow, EventRow.entry_id == EntryRow.id)
                .outerjoin(ScrapeCacheRow, ScrapeCacheRow.url == EntryRow.link)
                .where(EventRow.id == event_id)
            )
            result = row.one_or_none()
        if result is None:
            raise LookupError(f"event not found: {event_id}")
        event, entry, scrape = result
        return EventBundle(
            event=self._event_record(event),
            entry=self._entry_record(entry),
            scrape=self._scrape_record(scrape) if scrape is not None else None,
        )

    async def transition_event(
        self, event_id: int, current: EventStatus, target: EventStatus, **updates: object
    ) -> bool:
        assert_transition(current, target)
        valid_updates = set(EventRow.__table__.columns.keys()) - {
            "id",
            "status",
            "created_at",
            "updated_at",
        }
        unknown = set(updates) - valid_updates
        if unknown:
            raise ValueError(f"unsupported event updates: {', '.join(sorted(unknown))}")
        values = {"status": target.value, "updated_at": datetime.now(UTC), **updates}
        statement = (
            update(EventRow)
            .where(EventRow.id == event_id, EventRow.status == current.value)
            .values(**values)
        )
        async with self._session_factory.begin() as session:
            result = await session.execute(statement)
        return result.rowcount == 1

    async def recover_in_progress(self) -> int:
        in_progress = (
            EventStatus.SCREENING.value,
            EventStatus.FETCHING.value,
            EventStatus.SUMMARIZING.value,
            EventStatus.DELIVERING.value,
        )
        now = datetime.now(UTC)
        statement = (
            update(EventRow)
            .where(EventRow.status.in_(in_progress))
            .values(
                status=EventStatus.RETRY_WAIT.value,
                resume_stage=EventRow.status,
                next_attempt_at=now,
                updated_at=now,
            )
        )
        async with self._session_factory.begin() as session:
            result = await session.execute(statement)
        return result.rowcount

    async def save_scrape(
        self, url: str, markdown: str, content_hash: str, fetched_at: datetime
    ) -> None:
        statement = insert(ScrapeCacheRow).values(
            url=url,
            markdown=markdown,
            content_hash=content_hash,
            fetched_at=fetched_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=("url",),
            set_={
                "markdown": statement.excluded.markdown,
                "content_hash": statement.excluded.content_hash,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def get_scrape(self, url: str) -> ScrapeRecord | None:
        async with self._session_factory() as session:
            row = await session.get(ScrapeCacheRow, url)
        return self._scrape_record(row) if row is not None else None

    async def create_delivery(self, event_id: int, apprise_key: str) -> DeliveryRecord:
        now = datetime.now(UTC)
        idempotency_key = hashlib.sha256(f"{event_id}:{apprise_key}".encode()).hexdigest()
        statement = (
            insert(DeliveryRow)
            .values(
                event_id=event_id,
                apprise_key=apprise_key,
                idempotency_key=idempotency_key,
                status="pending",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=("event_id", "apprise_key"))
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)
            row = await session.scalar(
                select(DeliveryRow).where(
                    DeliveryRow.event_id == event_id, DeliveryRow.apprise_key == apprise_key
                )
            )
        if row is None:
            raise RuntimeError("delivery insert did not produce a row")
        return self._delivery_record(row)

    async def mark_delivery_success(self, delivery_id: int, response_summary: str) -> None:
        statement = (
            update(DeliveryRow)
            .where(DeliveryRow.id == delivery_id)
            .values(
                status="delivered",
                attempts=DeliveryRow.attempts + 1,
                response_summary=response_summary[:1000],
                updated_at=datetime.now(UTC),
            )
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def schedule_event_retry(
        self, event_id: int, failed_stage: EventStatus, error: str
    ) -> None:
        async with self._session_factory.begin() as session:
            row = await session.get(EventRow, event_id)
            if row is None:
                raise LookupError(f"event not found: {event_id}")
            if EventStatus(row.status) is not failed_stage:
                return
            attempt = row.failure_count + 1
            now = datetime.now(UTC)
            if attempt >= 5:
                target = EventStatus.FAILED
                next_attempt = None
                resume_stage = None
            else:
                assert_transition(failed_stage, EventStatus.RETRY_WAIT)
                target = EventStatus.RETRY_WAIT
                next_attempt = next_retry_at(now, attempt)
                resume_stage = failed_stage.value
            row.status = target.value
            row.resume_stage = resume_stage
            row.failure_count = attempt
            row.last_error = error[:1000]
            row.next_attempt_at = next_attempt
            row.updated_at = now

    async def resume_event(self, event_id: int) -> None:
        async with self._session_factory.begin() as session:
            row = await session.get(EventRow, event_id)
            if row is None:
                raise LookupError(f"event not found: {event_id}")
            if EventStatus(row.status) is not EventStatus.RETRY_WAIT or row.resume_stage is None:
                return
            target = EventStatus(row.resume_stage)
            assert_transition(EventStatus.RETRY_WAIT, target)
            row.status = target.value
            row.resume_stage = None
            row.next_attempt_at = None
            row.updated_at = datetime.now(UTC)

    async def make_event_due(self, event_id: int) -> None:
        statement = (
            update(EventRow)
            .where(
                EventRow.id == event_id,
                EventRow.status == EventStatus.RETRY_WAIT.value,
            )
            .values(next_attempt_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def list_due_event_ids(self, now: datetime, limit: int) -> list[int]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(EventRow.id)
                .where(
                    (EventRow.status == EventStatus.DISCOVERED.value)
                    | (
                        (EventRow.status == EventStatus.RETRY_WAIT.value)
                        & (EventRow.next_attempt_at <= now)
                    )
                )
                .order_by(EventRow.created_at)
                .limit(limit)
            )
            return list(rows)

    async def count_deliveries(self) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(DeliveryRow))
        return int(count or 0)
