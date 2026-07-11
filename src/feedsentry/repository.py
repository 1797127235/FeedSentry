from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from feedsentry.database import EntryRow, FeedStateRow, MonitorEventRow
from feedsentry.domain import EventStatus, assert_transition


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
class EventRecord:
    id: int
    monitor_id: str
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


class Repository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def feed_is_initialized(self, monitor_id: str, source_url: str) -> bool:
        async with self._session_factory() as session:
            result = await session.scalar(
                select(FeedStateRow.initialized_at).where(
                    FeedStateRow.monitor_id == monitor_id,
                    FeedStateRow.source_url == source_url,
                )
            )
        return result is not None

    async def mark_feed_initialized(
        self, monitor_id: str, source_url: str, initialized_at: datetime
    ) -> None:
        statement = (
            insert(FeedStateRow)
            .values(
                monitor_id=monitor_id,
                source_url=source_url,
                initialized_at=initialized_at,
                consecutive_failures=0,
            )
            .on_conflict_do_nothing(index_elements=("monitor_id", "source_url"))
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
    ) -> int:
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
            entry_id = await session.scalar(
                select(EntryRow.id).where(
                    EntryRow.source_url == source_url,
                    EntryRow.external_id == external_id,
                )
            )
        if entry_id is None:
            raise RuntimeError("entry insert did not produce an id")
        return entry_id

    async def create_event(
        self, monitor_id: str, entry_id: int, goal: str, goal_digest: str
    ) -> int:
        now = datetime.now(UTC)
        statement = (
            insert(MonitorEventRow)
            .values(
                monitor_id=monitor_id,
                entry_id=entry_id,
                status=EventStatus.DISCOVERED.value,
                goal_snapshot=goal,
                goal_hash=goal_digest,
                failure_count=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=("monitor_id", "entry_id"))
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)
            event_id = await session.scalar(
                select(MonitorEventRow.id).where(
                    MonitorEventRow.monitor_id == monitor_id,
                    MonitorEventRow.entry_id == entry_id,
                )
            )
        if event_id is None:
            raise RuntimeError("event insert did not produce an id")
        return event_id

    async def get_event(self, event_id: int) -> EventRecord:
        async with self._session_factory() as session:
            row = await session.get(MonitorEventRow, event_id)
        if row is None:
            raise LookupError(f"event not found: {event_id}")
        return EventRecord(
            id=row.id,
            monitor_id=row.monitor_id,
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

    async def transition_event(
        self, event_id: int, current: EventStatus, target: EventStatus, **updates: object
    ) -> bool:
        assert_transition(current, target)
        valid_updates = set(MonitorEventRow.__table__.columns.keys()) - {
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
            update(MonitorEventRow)
            .where(MonitorEventRow.id == event_id, MonitorEventRow.status == current.value)
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
            update(MonitorEventRow)
            .where(MonitorEventRow.status.in_(in_progress))
            .values(
                status=EventStatus.RETRY_WAIT.value,
                resume_stage=MonitorEventRow.status,
                next_attempt_at=now,
                updated_at=now,
            )
        )
        async with self._session_factory.begin() as session:
            result = await session.execute(statement)
        return result.rowcount
