from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Store datetimes as UTC-naive SQLite values and restore UTC awareness."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class FeedStateRow(Base):
    __tablename__ = "feed_state"

    monitor_id: Mapped[str] = mapped_column(primary_key=True)
    source_url: Mapped[str] = mapped_column(primary_key=True)
    etag: Mapped[str | None]
    last_modified: Mapped[str | None]
    initialized_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None]


class EntryRow(Base):
    __tablename__ = "entries"
    __table_args__ = (UniqueConstraint("source_url", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_url: Mapped[str] = mapped_column(index=True)
    external_id: Mapped[str]
    title: Mapped[str]
    summary: Mapped[str]
    link: Mapped[str]
    author: Mapped[str | None]
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    content_hash: Mapped[str]
    raw_json: Mapped[str]
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class MonitorEventRow(Base):
    __tablename__ = "monitor_events"
    __table_args__ = (UniqueConstraint("monitor_id", "entry_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    monitor_id: Mapped[str] = mapped_column(index=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("entries.id"))
    status: Mapped[str] = mapped_column(index=True)
    resume_stage: Mapped[str | None]
    goal_snapshot: Mapped[str]
    goal_hash: Mapped[str]
    decision_reason: Mapped[str | None]
    output_title: Mapped[str | None]
    output_summary: Mapped[str | None]
    failure_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ScrapeCacheRow(Base):
    __tablename__ = "scrape_cache"

    url: Mapped[str] = mapped_column(primary_key=True)
    markdown: Mapped[str]
    content_hash: Mapped[str]
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime())


class DeliveryRow(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "apprise_key"),
        UniqueConstraint("idempotency_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("monitor_events.id"))
    apprise_key: Mapped[str]
    idempotency_key: Mapped[str]
    status: Mapped[str]
    attempts: Mapped[int] = mapped_column(default=0)
    response_summary: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        database_url = f"sqlite+aiosqlite:///{path.resolve().as_posix()}"
        self.engine: AsyncEngine = create_async_engine(database_url)
        event.listen(self.engine.sync_engine, "connect", self._enable_foreign_keys)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as connection:
            await connection.execute(text("PRAGMA journal_mode=WAL"))
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


def create_database(path: Path) -> Database:
    return Database(path)
