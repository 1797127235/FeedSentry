# FeedSentry MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-process, self-hosted Python service that monitors RSS/RSSHub feeds, applies AI screening with optional Firecrawl enrichment, and sends accepted items through Apprise API without losing or duplicating work.

**Architecture:** A FastAPI process owns a background scheduler and a durable per-item state machine. YAML remains the configuration source of truth, while SQLite stores feed baselines, normalized entries, processing events, scrape cache, and delivery attempts. All external systems are accessed through small async clients so the pipeline can be tested with deterministic HTTP fakes.

**Tech Stack:** Python 3.12, Pydantic 2, PyYAML, SQLAlchemy 2 async, aiosqlite, httpx, feedparser, FastAPI, Uvicorn, pytest, pytest-asyncio, respx, Ruff, uv.

---

## File Map

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, runtime/dev dependencies, entry point, pytest and Ruff settings |
| `src/feedsentry/config.py` | YAML interpolation, typed configuration, hot reload, redaction |
| `src/feedsentry/domain.py` | Stable domain enums and value objects shared by all layers |
| `src/feedsentry/database.py` | SQLAlchemy tables, engine creation, WAL initialization |
| `src/feedsentry/repository.py` | Transactions, idempotent inserts, event claiming, retries, recovery, status counts |
| `src/feedsentry/feeds.py` | HTTP conditional requests, Feed parsing, URL normalization, entry fingerprints |
| `src/feedsentry/ai.py` | OpenAI-compatible request construction and structured decision validation |
| `src/feedsentry/firecrawl.py` | Optional-auth Firecrawl scrape client |
| `src/feedsentry/apprise.py` | Stateful Apprise API notification client |
| `src/feedsentry/ingestion.py` | Cold-start baseline and event creation for one monitor/source pair |
| `src/feedsentry/processor.py` | Durable event state machine and stage-specific retry handling |
| `src/feedsentry/scheduler.py` | Config reload, due-source polling, due-event processing, lifecycle |
| `src/feedsentry/logging.py` | JSON logs and secret redaction filter |
| `src/feedsentry/api.py` | Liveness, readiness, and status endpoints |
| `src/feedsentry/app.py` | Dependency wiring, FastAPI lifespan, CLI entry point |
| `config.example.yaml` | Runnable configuration example |
| `Dockerfile`, `compose.yaml` | FeedSentry-only container deployment |
| `tests/` | Unit, integration, and end-to-end coverage mirroring the modules above |

## Task 1: Bootstrap the Python Package

**Files:**
- Create: `pyproject.toml`
- Create: `src/feedsentry/__init__.py`
- Create: `tests/test_package.py`

- [ ] **Step 1: Write the failing package smoke test**

```python
# tests/test_package.py
import feedsentry


def test_package_exposes_version() -> None:
    assert feedsentry.__version__ == "0.1.0"
```

- [ ] **Step 2: Run the test to verify collection fails**

Run: `uv run pytest tests/test_package.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'feedsentry'`.

- [ ] **Step 3: Create package metadata and the minimal package**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "feedsentry"
version = "0.1.0"
description = "AI-assisted RSS monitoring and notification service"
requires-python = ">=3.12"
dependencies = [
  "aiosqlite>=0.21,<1",
  "fastapi>=0.116,<1",
  "feedparser>=6.0.11,<7",
  "httpx>=0.28,<1",
  "pydantic>=2.11,<3",
  "PyYAML>=6.0.2,<7",
  "sqlalchemy>=2.0.41,<3",
  "uvicorn[standard]>=0.35,<1",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.4,<9",
  "pytest-asyncio>=1.0,<2",
  "respx>=0.22,<1",
  "ruff>=0.12,<1",
]

[project.scripts]
feedsentry = "feedsentry.app:run"

[tool.hatch.build.targets.wheel]
packages = ["src/feedsentry"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]
```

```python
# src/feedsentry/__init__.py
__version__ = "0.1.0"
```

- [ ] **Step 4: Install dependencies and run quality checks**

Run: `uv sync --extra dev && uv run pytest tests/test_package.py -v && uv run ruff check .`

Expected: one test passes and Ruff reports `All checks passed!`.

- [ ] **Step 5: Commit the package scaffold**

```bash
git add pyproject.toml uv.lock src/feedsentry/__init__.py tests/test_package.py
git commit -m "build: scaffold FeedSentry Python package"
```

## Task 2: Typed YAML Configuration and Safe Reload

**Files:**
- Create: `src/feedsentry/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write tests for interpolation, validation, redaction, and reload fallback**

```python
# tests/test_config.py
from pathlib import Path

import pytest
from pydantic import ValidationError

from feedsentry.config import ConfigManager, load_config, redact_mapping


VALID_CONFIG = """
integrations:
  firecrawl:
    base_url: ${FIRECRAWL_URL}
    api_key: ${FIRECRAWL_KEY:-}
  apprise:
    base_url: http://apprise:8000
ai:
  base_url: http://llm:8080/v1
  api_key: secret-ai-key
  model: test-model
storage:
  path: ./data/test.db
monitors:
  - id: releases
    name: Releases
    goal: Important releases only
    interval: 10m
    sources: [https://example.com/feed.xml]
    destination: {apprise_key: telegram}
"""


def test_load_config_interpolates_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    config = load_config(path)
    assert str(config.integrations.firecrawl.base_url) == "http://firecrawl:3002/"
    assert config.integrations.firecrawl.api_key is None
    assert config.monitors[0].interval_seconds == 600


def test_duplicate_monitor_ids_are_rejected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    path = tmp_path / "config.yaml"
    duplicate = VALID_CONFIG + """
  - id: releases
    name: Duplicate
    goal: Another goal
    interval: 5m
    sources: [https://example.com/other.xml]
    destination: {apprise_key: email}
"""
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises((ValidationError, ValueError)):
        load_config(path)


def test_invalid_interval_is_rejected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG.replace("10m", "often"), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_redaction_masks_nested_secrets() -> None:
    value = {"api_key": "abc", "nested": {"password": "xyz", "model": "m"}}
    assert redact_mapping(value) == {
        "api_key": "***",
        "nested": {"password": "***", "model": "m"},
    }


def test_reload_keeps_last_good_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG, encoding="utf-8")
    manager = ConfigManager(path)
    original = manager.load_initial()
    path.write_text("monitors: [", encoding="utf-8")
    assert manager.reload_if_changed() is False
    assert manager.current is original
    assert manager.last_error is not None
```

- [ ] **Step 2: Run the tests to verify the configuration module is absent**

Run: `uv run pytest tests/test_config.py -v`

Expected: FAIL during collection because `feedsentry.config` does not exist.

- [ ] **Step 3: Implement typed models and duration parsing**

Create `src/feedsentry/config.py` with these public types and validators:

```python
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
SECRET_KEYS = {"api_key", "password", "token", "secret"}


def parse_duration(value: str) -> int:
    match = re.fullmatch(r"([1-9]\d*)([smhd])", value.strip())
    if match is None:
        raise ValueError("interval must use s, m, h, or d, for example 10m")
    amount, unit = int(match.group(1)), match.group(2)
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


class FirecrawlConfig(BaseModel):
    base_url: HttpUrl
    api_key: str | None = None


class AppriseConfig(BaseModel):
    base_url: HttpUrl


class IntegrationsConfig(BaseModel):
    firecrawl: FirecrawlConfig
    apprise: AppriseConfig


class AIConfig(BaseModel):
    base_url: HttpUrl
    api_key: str
    model: str = Field(min_length=1)


class StorageConfig(BaseModel):
    path: Path


class DestinationConfig(BaseModel):
    apprise_key: str = Field(pattern=r"^[A-Za-z0-9._-]+$")


class MonitorConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    interval: str
    sources: list[HttpUrl] = Field(min_length=1)
    destination: DestinationConfig
    enabled: bool = True

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, value: str) -> str:
        parse_duration(value)
        return value

    @property
    def interval_seconds(self) -> int:
        return parse_duration(self.interval)


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    integrations: IntegrationsConfig
    ai: AIConfig
    storage: StorageConfig
    monitors: list[MonitorConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_monitor_ids(self) -> "AppConfig":
        ids = [monitor.id for monitor in self.monitors]
        if len(ids) != len(set(ids)):
            raise ValueError("monitor ids must be unique")
        return self
```

- [ ] **Step 4: Implement interpolation, loading, reload fallback, and redaction**

Append these complete functions/classes to `src/feedsentry/config.py`:

```python
def _expand_environment(raw: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ValueError(f"missing environment variable: {name}")

    return ENV_PATTERN.sub(replace, raw)


def load_config(path: Path) -> AppConfig:
    expanded = _expand_environment(path.read_text(encoding="utf-8"))
    payload = yaml.safe_load(expanded)
    if not isinstance(payload, dict):
        raise ValueError("configuration root must be a mapping")
    return AppConfig.model_validate(payload)


def redact_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if key.lower() in SECRET_KEYS else redact_mapping(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    return value


class ConfigManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.current: AppConfig | None = None
        self.last_error: str | None = None
        self._mtime_ns: int | None = None

    def load_initial(self) -> AppConfig:
        self.current = load_config(self.path)
        self._mtime_ns = self.path.stat().st_mtime_ns
        self.last_error = None
        return self.current

    def reload_if_changed(self) -> bool:
        mtime_ns = self.path.stat().st_mtime_ns
        if mtime_ns == self._mtime_ns:
            return False
        try:
            candidate = load_config(self.path)
        except Exception as exc:
            self.last_error = str(exc)
            self._mtime_ns = mtime_ns
            return False
        self.current = candidate
        self._mtime_ns = mtime_ns
        self.last_error = None
        return True
```

- [ ] **Step 5: Run configuration tests and the full suite**

Run: `uv run pytest tests/test_config.py -v && uv run pytest -q && uv run ruff check .`

Expected: all tests pass and Ruff reports no findings.

- [ ] **Step 6: Commit typed configuration**

```bash
git add src/feedsentry/config.py tests/test_config.py
git commit -m "feat: add validated YAML configuration"
```

## Task 3: Domain State Machine and Retry Policy

**Files:**
- Create: `src/feedsentry/domain.py`
- Create: `tests/test_domain.py`

- [ ] **Step 1: Write tests for legal transitions, retry timing, and stable hashes**

```python
# tests/test_domain.py
from datetime import UTC, datetime, timedelta

import pytest

from feedsentry.domain import (
    DecisionAction,
    EventStatus,
    ScreeningDecision,
    assert_transition,
    goal_hash,
    next_retry_at,
)


def test_screening_decision_requires_summary_when_accepted() -> None:
    with pytest.raises(ValueError):
        ScreeningDecision(action=DecisionAction.ACCEPT, reason="relevant")


def test_transition_rejects_skipping_screening() -> None:
    with pytest.raises(ValueError, match="invalid event transition"):
        assert_transition(EventStatus.DISCOVERED, EventStatus.DELIVERED)


def test_retry_schedule_has_four_delays_then_stops() -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    assert next_retry_at(now, 1) == now + timedelta(minutes=1)
    assert next_retry_at(now, 4) == now + timedelta(hours=2)
    assert next_retry_at(now, 5) is None


def test_goal_hash_is_stable() -> None:
    assert goal_hash("  Important releases\n") == goal_hash("Important releases")
```

- [ ] **Step 2: Run the tests to verify the domain module is absent**

Run: `uv run pytest tests/test_domain.py -v`

Expected: FAIL during collection because `feedsentry.domain` does not exist.

- [ ] **Step 3: Implement enums, structured decisions, transitions, and retry policy**

```python
# src/feedsentry/domain.py
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class DecisionAction(StrEnum):
    DISCARD = "discard"
    ACCEPT = "accept"
    FETCH = "fetch"


class EventStatus(StrEnum):
    DISCOVERED = "discovered"
    SCREENING = "screening"
    FILTERED = "filtered"
    FETCHING = "fetching"
    SUMMARIZING = "summarizing"
    DELIVERY_PENDING = "delivery_pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class ScreeningDecision(BaseModel):
    action: DecisionAction
    reason: str = Field(min_length=1)
    title: str | None = None
    summary: str | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "ScreeningDecision":
        if self.action is DecisionAction.ACCEPT and not self.summary:
            raise ValueError("accepted decisions require a summary")
        if self.action is DecisionAction.FETCH and self.summary is not None:
            raise ValueError("fetch decisions cannot include a final summary")
        return self


ALLOWED_TRANSITIONS = {
    EventStatus.DISCOVERED: {EventStatus.SCREENING},
    EventStatus.SCREENING: {
        EventStatus.FILTERED,
        EventStatus.FETCHING,
        EventStatus.DELIVERY_PENDING,
        EventStatus.RETRY_WAIT,
    },
    EventStatus.FETCHING: {EventStatus.SUMMARIZING, EventStatus.RETRY_WAIT},
    EventStatus.SUMMARIZING: {
        EventStatus.FILTERED,
        EventStatus.DELIVERY_PENDING,
        EventStatus.RETRY_WAIT,
    },
    EventStatus.DELIVERY_PENDING: {EventStatus.DELIVERING},
    EventStatus.DELIVERING: {EventStatus.DELIVERED, EventStatus.RETRY_WAIT},
    EventStatus.RETRY_WAIT: {
        EventStatus.SCREENING,
        EventStatus.FETCHING,
        EventStatus.SUMMARIZING,
        EventStatus.DELIVERING,
        EventStatus.FAILED,
    },
}


def assert_transition(current: EventStatus, target: EventStatus) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid event transition: {current} -> {target}")


RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))


def next_retry_at(now: datetime, failure_count: int) -> datetime | None:
    if failure_count < 1 or failure_count > len(RETRY_DELAYS):
        return None
    return now + RETRY_DELAYS[failure_count - 1]


def goal_hash(goal: str) -> str:
    normalized = " ".join(goal.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run domain tests and lint**

Run: `uv run pytest tests/test_domain.py -v && uv run ruff check src/feedsentry/domain.py tests/test_domain.py`

Expected: four tests pass and Ruff reports no findings.

- [ ] **Step 5: Commit the domain model**

```bash
git add src/feedsentry/domain.py tests/test_domain.py
git commit -m "feat: define durable event state machine"
```

## Task 4: SQLite Schema and Repository Primitives

**Files:**
- Create: `src/feedsentry/database.py`
- Create: `src/feedsentry/repository.py`
- Create: `tests/test_repository.py`

- [ ] **Step 1: Write repository tests for WAL, baselines, idempotency, and recovery**

```python
# tests/test_repository.py
from datetime import UTC, datetime

import pytest

from feedsentry.database import create_database
from feedsentry.domain import EventStatus
from feedsentry.repository import Repository


@pytest.fixture
async def repository(tmp_path):
    database = create_database(tmp_path / "feedsentry.db")
    await database.initialize()
    yield Repository(database.session_factory)
    await database.dispose()


async def test_feed_baseline_is_scoped_to_monitor(repository: Repository) -> None:
    now = datetime.now(UTC)
    await repository.mark_feed_initialized("monitor-a", "https://example.com/feed", now)
    assert await repository.feed_is_initialized("monitor-a", "https://example.com/feed")
    assert not await repository.feed_is_initialized("monitor-b", "https://example.com/feed")


async def test_event_insert_is_idempotent(repository: Repository) -> None:
    entry_id = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="item-1",
        title="One",
        summary="Summary",
        link="https://example.com/1",
        author=None,
        published_at=None,
        content_hash="hash-1",
        raw_json="{}",
    )
    first = await repository.create_event("monitor-a", entry_id, "goal", "goal-hash")
    second = await repository.create_event("monitor-a", entry_id, "goal", "goal-hash")
    assert first == second


async def test_recovery_returns_in_progress_events_to_retry(repository: Repository) -> None:
    entry_id = await repository.upsert_entry(
        source_url="https://example.com/feed",
        external_id="item-2",
        title="Two",
        summary="Summary",
        link="https://example.com/2",
        author=None,
        published_at=None,
        content_hash="hash-2",
        raw_json="{}",
    )
    event_id = await repository.create_event("monitor-a", entry_id, "goal", "goal-hash")
    await repository.transition_event(event_id, EventStatus.DISCOVERED, EventStatus.SCREENING)
    await repository.transition_event(event_id, EventStatus.SCREENING, EventStatus.FETCHING)
    await repository.recover_in_progress()
    event = await repository.get_event(event_id)
    assert event.status is EventStatus.RETRY_WAIT
    assert event.resume_stage is EventStatus.FETCHING
```

- [ ] **Step 2: Run tests to verify database modules are absent**

Run: `uv run pytest tests/test_repository.py -v`

Expected: FAIL during collection because `feedsentry.database` does not exist.

- [ ] **Step 3: Implement SQLAlchemy tables and database initialization**

Create `src/feedsentry/database.py` with SQLAlchemy 2 declarative models for exactly these columns:

```python
class FeedStateRow(Base):
    __tablename__ = "feed_state"
    monitor_id: Mapped[str] = mapped_column(primary_key=True)
    source_url: Mapped[str] = mapped_column(primary_key=True)
    etag: Mapped[str | None]
    last_modified: Mapped[str | None]
    initialized_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    next_check_at: Mapped[datetime | None]
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
    published_at: Mapped[datetime | None]
    content_hash: Mapped[str]
    raw_json: Mapped[str]
    first_seen_at: Mapped[datetime] = mapped_column(index=True)


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
    next_attempt_at: Mapped[datetime | None] = mapped_column(index=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ScrapeCacheRow(Base):
    __tablename__ = "scrape_cache"
    url: Mapped[str] = mapped_column(primary_key=True)
    markdown: Mapped[str]
    content_hash: Mapped[str]
    fetched_at: Mapped[datetime]


class DeliveryRow(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("event_id", "apprise_key"), UniqueConstraint("idempotency_key"))
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("monitor_events.id"))
    apprise_key: Mapped[str]
    idempotency_key: Mapped[str]
    status: Mapped[str]
    attempts: Mapped[int] = mapped_column(default=0)
    response_summary: Mapped[str | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

Also implement `Database.initialize()` to create parent directories, create tables, and execute `PRAGMA journal_mode=WAL`; expose `session_factory` and `dispose()`.

- [ ] **Step 4: Implement repository methods used by the tests**

In `src/feedsentry/repository.py`, implement a `Repository` with these exact async methods and transaction boundaries:

```python
async def feed_is_initialized(self, monitor_id: str, source_url: str) -> bool
async def mark_feed_initialized(self, monitor_id: str, source_url: str, initialized_at: datetime) -> None
async def upsert_entry(
    self, *, source_url: str, external_id: str, title: str, summary: str,
    link: str, author: str | None, published_at: datetime | None,
    content_hash: str, raw_json: str,
) -> int
async def create_event(self, monitor_id: str, entry_id: int, goal: str, goal_digest: str) -> int
async def get_event(self, event_id: int) -> EventRecord
async def transition_event(
    self, event_id: int, current: EventStatus, target: EventStatus, **updates: object
) -> bool
async def recover_in_progress(self) -> int
```

Define frozen `EventRecord` and `EntryRecord` dataclasses in `repository.py`. Use SQLite `INSERT ... ON CONFLICT DO NOTHING`, then select the stable row ID. `recover_in_progress()` must move `SCREENING`, `FETCHING`, `SUMMARIZING`, and `DELIVERING` rows to `RETRY_WAIT`, preserving the former status in `resume_stage` and setting `next_attempt_at` to the current UTC time.

- [ ] **Step 5: Run repository tests and inspect schema constraints**

Run: `uv run pytest tests/test_repository.py -v && uv run ruff check src/feedsentry/database.py src/feedsentry/repository.py tests/test_repository.py`

Expected: three tests pass; the SQLite file is created under pytest's temporary directory; Ruff reports no findings.

- [ ] **Step 6: Commit durable storage primitives**

```bash
git add src/feedsentry/database.py src/feedsentry/repository.py tests/test_repository.py
git commit -m "feat: add SQLite event repository"
```

## Task 5: Feed Fetching and Normalization

**Files:**
- Create: `src/feedsentry/feeds.py`
- Create: `tests/test_feeds.py`

- [ ] **Step 1: Write parsing, fingerprint, and conditional request tests**

```python
# tests/test_feeds.py
import httpx
import respx

from feedsentry.feeds import FeedClient, normalize_feed


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><guid>one</guid><title>Release 1</title><link>https://example.com/one</link>
<description>Details</description></item></channel></rss>"""


def test_normalize_feed_uses_guid_and_stable_hash() -> None:
    first = normalize_feed(RSS, "https://example.com/feed.xml")[0]
    second = normalize_feed(RSS, "https://example.com/feed.xml")[0]
    assert first.external_id == "one"
    assert first.content_hash == second.content_hash


@respx.mock
async def test_fetch_sends_conditional_headers() -> None:
    route = respx.get("https://example.com/feed.xml").mock(
        return_value=httpx.Response(304)
    )
    async with httpx.AsyncClient() as http:
        result = await FeedClient(http).fetch(
            "https://example.com/feed.xml", etag='"abc"', last_modified="yesterday"
        )
    assert result.not_modified is True
    assert route.calls[0].request.headers["if-none-match"] == '"abc"'
    assert route.calls[0].request.headers["if-modified-since"] == "yesterday"
```

- [ ] **Step 2: Run tests to verify the Feed module is absent**

Run: `uv run pytest tests/test_feeds.py -v`

Expected: FAIL during collection because `feedsentry.feeds` does not exist.

- [ ] **Step 3: Implement normalized values and Feed parsing**

Create immutable `NormalizedEntry` and `FeedFetchResult` dataclasses in `src/feedsentry/feeds.py`. Implement `normalize_feed(content: bytes, source_url: str)` using `feedparser.loads`. Select external ID in this order: `id`, normalized `link`, SHA-256 of normalized title/summary/published fields. Compute `content_hash` from normalized title, summary, link, author, and published value. Serialize each original feed entry with `json.dumps(entry, default=str, ensure_ascii=False, sort_keys=True)`.

- [ ] **Step 4: Implement the async Feed client**

```python
class FeedClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def fetch(
        self, source_url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FeedFetchResult:
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        response = await self.http.get(source_url, headers=headers, follow_redirects=True)
        if response.status_code == 304:
            return FeedFetchResult(not_modified=True, etag=etag, last_modified=last_modified, entries=())
        response.raise_for_status()
        entries = tuple(normalize_feed(response.content, source_url))
        return FeedFetchResult(
            not_modified=False,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            entries=entries,
        )
```

Set a 20-second HTTP timeout when wiring the shared `httpx.AsyncClient` later, not inside this client.

- [ ] **Step 5: Run Feed tests and the full suite**

Run: `uv run pytest tests/test_feeds.py -v && uv run pytest -q && uv run ruff check .`

Expected: all tests pass and Ruff reports no findings.

- [ ] **Step 6: Commit Feed collection**

```bash
git add src/feedsentry/feeds.py tests/test_feeds.py
git commit -m "feat: add conditional RSS feed client"
```

## Task 6: OpenAI-Compatible Screening Client

**Files:**
- Create: `src/feedsentry/ai.py`
- Create: `tests/test_ai.py`

- [ ] **Step 1: Write tests for request shape and invalid structured output**

```python
# tests/test_ai.py
import httpx
import pytest
import respx
from pydantic import ValidationError

from feedsentry.ai import AIClient
from feedsentry.domain import DecisionAction


@respx.mock
async def test_screen_parses_accept_decision() -> None:
    route = respx.post("http://llm/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"action":"accept","reason":"major release","title":"V2","summary":"Adds durable workflows"}'}}]},
        )
    )
    async with httpx.AsyncClient() as http:
        client = AIClient(http, "http://llm/v1", "key", "model")
        decision = await client.screen("Watch major releases", "V2", "Release notes")
    assert decision.action is DecisionAction.ACCEPT
    assert route.calls[0].request.headers["authorization"] == "Bearer key"


@respx.mock
async def test_screen_rejects_invalid_model_output() -> None:
    respx.post("http://llm/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})
    )
    async with httpx.AsyncClient() as http:
        client = AIClient(http, "http://llm/v1", "key", "model")
        with pytest.raises((ValueError, ValidationError)):
            await client.screen("goal", "title", "summary")
```

- [ ] **Step 2: Run tests to verify the AI module is absent**

Run: `uv run pytest tests/test_ai.py -v`

Expected: FAIL during collection because `feedsentry.ai` does not exist.

- [ ] **Step 3: Implement prompts and the OpenAI-compatible client**

Create `src/feedsentry/ai.py` with:

```python
SCREEN_SYSTEM_PROMPT = """You screen feed items against a user's monitoring goal.
Return one JSON object only. action must be discard, accept, or fetch.
Use fetch only when the item may be relevant but the feed text is insufficient.
For accept, include title, a concise summary, and a concrete relevance reason.
For discard, explain briefly why it is outside the goal."""

FINAL_SYSTEM_PROMPT = """You evaluate fetched source content against a monitoring goal.
Return one JSON object only. action must be discard or accept.
For accept, include title, a concise factual summary, and a concrete relevance reason.
Never request another fetch and do not use facts absent from the supplied content."""
```

Implement `AIClient.screen(goal, title, feed_summary)` and `AIClient.summarize(goal, title, markdown)`. Both call a private `_complete(system_prompt, user_payload)` that posts to `{base_url}/chat/completions` with `model`, deterministic `temperature: 0`, and two chat messages. Parse `choices[0].message.content` with `json.loads`, then `ScreeningDecision.model_validate`. `summarize()` must reject `DecisionAction.FETCH` even if the model returns it.

- [ ] **Step 4: Run AI tests and lint**

Run: `uv run pytest tests/test_ai.py -v && uv run ruff check src/feedsentry/ai.py tests/test_ai.py`

Expected: two tests pass and Ruff reports no findings.

- [ ] **Step 5: Commit the AI client**

```bash
git add src/feedsentry/ai.py tests/test_ai.py
git commit -m "feat: add structured AI screening client"
```

## Task 7: Firecrawl and Apprise HTTP Clients

**Files:**
- Create: `src/feedsentry/firecrawl.py`
- Create: `src/feedsentry/apprise.py`
- Create: `tests/test_integrations.py`

- [ ] **Step 1: Write tests for optional Firecrawl auth and Apprise payloads**

```python
# tests/test_integrations.py
import httpx
import respx

from feedsentry.apprise import AppriseClient
from feedsentry.firecrawl import FirecrawlClient


@respx.mock
async def test_firecrawl_omits_auth_when_key_is_empty() -> None:
    route = respx.post("http://firecrawl:3002/v1/scrape").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"markdown": "Body"}})
    )
    async with httpx.AsyncClient() as http:
        body = await FirecrawlClient(http, "http://firecrawl:3002", None).scrape(
            "https://example.com/post"
        )
    assert body == "Body"
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
async def test_firecrawl_uses_bearer_auth_when_configured() -> None:
    route = respx.post("http://firecrawl:3002/v1/scrape").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"markdown": "Body"}})
    )
    async with httpx.AsyncClient() as http:
        await FirecrawlClient(http, "http://firecrawl:3002", "token").scrape(
            "https://example.com/post"
        )
    assert route.calls[0].request.headers["authorization"] == "Bearer token"


@respx.mock
async def test_apprise_sends_expected_message() -> None:
    route = respx.post("http://apprise:8000/notify/telegram").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    async with httpx.AsyncClient() as http:
        await AppriseClient(http, "http://apprise:8000").notify(
            "telegram", "Release V2", "Summary\n\nWhy: relevant\n\nhttps://example.com/v2"
        )
    assert route.calls[0].request.content
    assert route.calls[0].request.headers["content-type"].startswith("application/json")
```

- [ ] **Step 2: Run tests to verify both clients are absent**

Run: `uv run pytest tests/test_integrations.py -v`

Expected: FAIL during collection because the integration modules do not exist.

- [ ] **Step 3: Implement the Firecrawl scrape client**

```python
# src/feedsentry/firecrawl.py
import httpx


class FirecrawlClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str | None) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def scrape(self, url: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = await self.http.post(
            f"{self.base_url}/v1/scrape",
            headers=headers,
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )
        response.raise_for_status()
        payload = response.json()
        markdown = payload.get("data", {}).get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("Firecrawl response did not contain markdown")
        return markdown
```

- [ ] **Step 4: Implement the Apprise notification client**

```python
# src/feedsentry/apprise.py
import httpx


class AppriseClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def notify(self, key: str, title: str, body: str) -> str:
        response = await self.http.post(
            f"{self.base_url}/notify/{key}",
            json={"title": title, "body": body, "type": "info"},
        )
        response.raise_for_status()
        return response.text[:1000]
```

- [ ] **Step 5: Run integration-client tests and the full suite**

Run: `uv run pytest tests/test_integrations.py -v && uv run pytest -q && uv run ruff check .`

Expected: three tests pass, the full suite remains green, and Ruff reports no findings.

- [ ] **Step 6: Commit external clients**

```bash
git add src/feedsentry/firecrawl.py src/feedsentry/apprise.py tests/test_integrations.py
git commit -m "feat: add Firecrawl and Apprise clients"
```

## Task 8: Cold-Start Baseline and Feed Ingestion

**Files:**
- Create: `src/feedsentry/ingestion.py`
- Modify: `src/feedsentry/repository.py`
- Modify: `tests/test_repository.py`
- Create: `tests/test_ingestion.py`

- [ ] **Step 1: Write tests for baseline suppression and later event creation**

```python
# tests/test_ingestion.py
from datetime import UTC, datetime, timedelta

from feedsentry.config import DestinationConfig, MonitorConfig
from feedsentry.feeds import FeedFetchResult, NormalizedEntry
from feedsentry.ingestion import IngestionService


def entry(external_id: str) -> NormalizedEntry:
    return NormalizedEntry(
        source_url="https://example.com/feed",
        external_id=external_id,
        title=external_id,
        summary="summary",
        link=f"https://example.com/{external_id}",
        author=None,
        published_at=None,
        content_hash=f"hash-{external_id}",
        raw_json="{}",
    )


async def test_first_fetch_creates_baseline_without_events(repository, fake_feed_client) -> None:
    fake_feed_client.result = FeedFetchResult(False, "etag", None, (entry("old"),))
    service = IngestionService(repository, fake_feed_client)
    created = await service.poll_monitor_source(make_monitor(), "https://example.com/feed")
    assert created == 0
    assert await repository.count_events() == 0


async def test_later_fetch_creates_one_event_per_new_entry(repository, fake_feed_client) -> None:
    monitor = make_monitor()
    fake_feed_client.result = FeedFetchResult(False, "e1", None, (entry("old"),))
    service = IngestionService(repository, fake_feed_client)
    await service.poll_monitor_source(monitor, "https://example.com/feed")
    fake_feed_client.result = FeedFetchResult(False, "e2", None, (entry("old"), entry("new")))
    created = await service.poll_monitor_source(monitor, "https://example.com/feed")
    assert created == 1
    assert await repository.count_events() == 1
```

Add fixtures `repository`, `fake_feed_client`, and `make_monitor()` to `tests/conftest.py`. `make_monitor()` returns a `MonitorConfig` with ID `monitor-a`, goal `Important releases`, interval `10m`, one source, and Apprise key `telegram`.

- [ ] **Step 2: Run ingestion tests to verify the service is absent**

Run: `uv run pytest tests/test_ingestion.py -v`

Expected: FAIL during collection because `feedsentry.ingestion` does not exist.

- [ ] **Step 3: Extend repository APIs for ingestion**

Add these methods to `Repository` and cover them in `tests/test_repository.py`:

```python
async def get_feed_state(self, monitor_id: str, source_url: str) -> FeedStateRecord | None
async def record_feed_success(
    self, monitor_id: str, source_url: str, *, etag: str | None,
    last_modified: str | None, checked_at: datetime, next_check_at: datetime,
) -> None
async def record_feed_failure(
    self, monitor_id: str, source_url: str, *, error: str,
    checked_at: datetime, next_check_at: datetime,
) -> None
async def source_is_due(
    self, monitor_id: str, source_url: str, now: datetime
) -> bool
async def count_events(self) -> int
```

Change `upsert_entry()` to return `EntryRecord`, including `first_seen_at`, rather than only the integer ID. Preserve the existing row's original `first_seen_at` on conflict.

- [ ] **Step 4: Implement ingestion with a per-monitor baseline**

```python
# src/feedsentry/ingestion.py
from datetime import UTC, datetime, timedelta

from feedsentry.config import MonitorConfig
from feedsentry.domain import goal_hash


class IngestionService:
    def __init__(self, repository, feed_client) -> None:
        self.repository = repository
        self.feed_client = feed_client

    async def poll_monitor_source(self, monitor: MonitorConfig, source_url: str) -> int:
        state = await self.repository.get_feed_state(monitor.id, source_url)
        result = await self.feed_client.fetch(
            source_url,
            etag=state.etag if state else None,
            last_modified=state.last_modified if state else None,
        )
        now = datetime.now(UTC)
        if result.not_modified:
            await self.repository.record_feed_success(
                monitor.id,
                source_url,
                etag=state.etag if state else None,
                last_modified=state.last_modified if state else None,
                checked_at=now,
                next_check_at=now + timedelta(seconds=monitor.interval_seconds),
            )
            return 0

        records = [await self.repository.upsert_entry(**item.as_repository_kwargs()) for item in result.entries]
        if state is None or state.initialized_at is None:
            await self.repository.mark_feed_initialized(monitor.id, source_url, now)
            created = 0
        else:
            created = 0
            for record in records:
                if record.first_seen_at > state.initialized_at:
                    await self.repository.create_event(
                        monitor.id, record.id, monitor.goal, goal_hash(monitor.goal)
                    )
                    created += 1
        await self.repository.record_feed_success(
            monitor.id,
            source_url,
            etag=result.etag,
            last_modified=result.last_modified,
            checked_at=now,
            next_check_at=now + timedelta(seconds=monitor.interval_seconds),
        )
        return created
```

Add `NormalizedEntry.as_repository_kwargs()` returning the exact keyword arguments expected by `Repository.upsert_entry()`.

- [ ] **Step 5: Add source-failure isolation test and implementation**

Test that a raised `httpx.HTTPError` calls `record_feed_failure()` with a bounded error string and re-raises no exception from `poll_monitor_source()`. Use source retry delays of 1, 5, 30, and 120 minutes based on `consecutive_failures`; cap later failures at 120 minutes.

- [ ] **Step 6: Run ingestion and repository tests**

Run: `uv run pytest tests/test_ingestion.py tests/test_repository.py -v && uv run ruff check .`

Expected: baseline, incremental event, and failure-isolation tests pass.

- [ ] **Step 7: Commit ingestion**

```bash
git add src/feedsentry/ingestion.py src/feedsentry/repository.py tests/conftest.py tests/test_ingestion.py tests/test_repository.py
git commit -m "feat: ingest feeds with cold-start baselines"
```

## Task 9: Durable Event Processor

**Files:**
- Create: `src/feedsentry/processor.py`
- Modify: `src/feedsentry/repository.py`
- Create: `tests/test_processor.py`

- [ ] **Step 1: Write tests for discard, fetch, accept, delivery, and retry resume**

```python
# tests/test_processor.py
from feedsentry.domain import DecisionAction, EventStatus, ScreeningDecision
from feedsentry.processor import EventProcessor


async def test_discard_finishes_without_delivery(processor_fixture) -> None:
    fixture = processor_fixture
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.DISCARD, reason="outside goal"
    )
    await fixture.processor.process_event(fixture.event_id)
    event = await fixture.repository.get_event(fixture.event_id)
    assert event.status is EventStatus.FILTERED
    assert await fixture.repository.count_deliveries() == 0


async def test_fetch_path_caches_content_and_delivers(processor_fixture) -> None:
    fixture = processor_fixture
    fixture.ai.screen_result = ScreeningDecision(action=DecisionAction.FETCH, reason="need details")
    fixture.ai.summary_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major capability",
        title="Release V2",
        summary="Adds durable workflows",
    )
    fixture.firecrawl.markdown = "Full release notes"
    await fixture.processor.process_event(fixture.event_id)
    event = await fixture.repository.get_event(fixture.event_id)
    assert event.status is EventStatus.DELIVERED
    assert fixture.firecrawl.calls == 1
    assert fixture.apprise.calls == 1


async def test_apprise_failure_retries_delivery_without_repeating_ai(processor_fixture) -> None:
    fixture = processor_fixture
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT,
        reason="major release",
        title="Release V2",
        summary="Adds durable workflows",
    )
    fixture.apprise.error = RuntimeError("temporary outage")
    await fixture.processor.process_event(fixture.event_id)
    waiting = await fixture.repository.get_event(fixture.event_id)
    assert waiting.status is EventStatus.RETRY_WAIT
    assert waiting.resume_stage is EventStatus.DELIVERING
    fixture.apprise.error = None
    await fixture.repository.make_event_due(fixture.event_id)
    await fixture.processor.process_event(fixture.event_id)
    delivered = await fixture.repository.get_event(fixture.event_id)
    assert delivered.status is EventStatus.DELIVERED
    assert fixture.ai.screen_calls == 1
```

- [ ] **Step 2: Run processor tests to verify the module is absent**

Run: `uv run pytest tests/test_processor.py -v`

Expected: FAIL during collection because `feedsentry.processor` does not exist.

- [ ] **Step 3: Add repository operations for atomic stage changes**

Implement and test these methods:

```python
async def get_event_bundle(self, event_id: int) -> EventBundle
async def transition_event(self, event_id: int, current: EventStatus, target: EventStatus, **updates) -> bool
async def save_scrape(self, url: str, markdown: str, content_hash: str, fetched_at: datetime) -> None
async def get_scrape(self, url: str) -> ScrapeRecord | None
async def create_delivery(self, event_id: int, apprise_key: str) -> DeliveryRecord
async def mark_delivery_success(self, delivery_id: int, response_summary: str) -> None
async def schedule_event_retry(self, event_id: int, failed_stage: EventStatus, error: str) -> None
async def resume_event(self, event_id: int) -> None
async def make_event_due(self, event_id: int) -> None
async def list_due_event_ids(self, now: datetime, limit: int) -> list[int]
async def count_deliveries(self) -> int
```

The `transition_event()` method added in Task 4 must update only when the persisted status equals `current`, preventing two workers from advancing the same event. `resume_event()` reads `resume_stage`, validates the `RETRY_WAIT -> resume_stage` transition, clears `next_attempt_at`, and writes the restored status in one transaction. `create_delivery()` computes SHA-256 of `event_id:apprise_key` and returns the existing row on conflict.

- [ ] **Step 4: Implement the processor stage loop**

Implement `EventProcessor.process_event(event_id)` as a loop that reloads the event bundle after every committed transition:

```python
async def process_event(self, event_id: int) -> None:
    while True:
        bundle = await self.repository.get_event_bundle(event_id)
        status = bundle.event.status
        if status in {EventStatus.FILTERED, EventStatus.DELIVERED, EventStatus.FAILED}:
            return
        if status is EventStatus.RETRY_WAIT:
            if bundle.event.next_attempt_at > self.clock():
                return
            await self.repository.resume_event(event_id)
            continue
        try:
            if status is EventStatus.DISCOVERED:
                await self.repository.transition_event(
                    event_id, EventStatus.DISCOVERED, EventStatus.SCREENING
                )
            elif status is EventStatus.SCREENING:
                await self._screen(bundle)
            elif status is EventStatus.FETCHING:
                await self._fetch(bundle)
            elif status is EventStatus.SUMMARIZING:
                await self._summarize(bundle)
            elif status is EventStatus.DELIVERY_PENDING:
                await self.repository.transition_event(
                    event_id, EventStatus.DELIVERY_PENDING, EventStatus.DELIVERING
                )
            elif status is EventStatus.DELIVERING:
                await self._deliver(bundle)
            else:
                raise RuntimeError(f"unsupported event status: {status}")
        except Exception as exc:
            await self.repository.schedule_event_retry(event_id, status, str(exc)[:1000])
            return
```

Implement `_screen`, `_fetch`, `_summarize`, and `_deliver` with the decisions and clients defined in earlier tasks. Build the notification body exactly as:

```python
body = f"{summary}\n\n为什么相关：{reason}\n\n{entry.link}"
```

Before calling Firecrawl, return cached markdown when available. Before calling Apprise, create/get the idempotent delivery row. On the fifth failure of one stage, `schedule_event_retry()` sets `FAILED` rather than `RETRY_WAIT`.

- [ ] **Step 5: Run processor tests and verify no duplicate expensive calls**

Run: `uv run pytest tests/test_processor.py -v && uv run pytest -q`

Expected: all processor paths pass, including the assertion that an Apprise retry does not repeat AI screening.

- [ ] **Step 6: Commit the durable processor**

```bash
git add src/feedsentry/processor.py src/feedsentry/repository.py tests/test_processor.py tests/conftest.py
git commit -m "feat: process monitoring events durably"
```

## Task 10: Scheduler, Recovery, and Application Wiring

**Files:**
- Create: `src/feedsentry/scheduler.py`
- Create: `src/feedsentry/app.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write scheduler tests for due work, reloads, and cancellation**

```python
# tests/test_scheduler.py
import asyncio

from feedsentry.scheduler import Scheduler


async def test_tick_reloads_config_polls_due_sources_and_processes_events(scheduler_fixture) -> None:
    fixture = scheduler_fixture
    await fixture.scheduler.tick()
    assert fixture.config.reload_calls == 1
    assert fixture.ingestion.polled == [("monitor-a", "https://example.com/feed")]
    assert fixture.processor.processed == [fixture.due_event_id]


async def test_run_stops_cleanly(scheduler_fixture) -> None:
    task = asyncio.create_task(scheduler_fixture.scheduler.run())
    await asyncio.sleep(0)
    await scheduler_fixture.scheduler.stop()
    await task
    assert task.done()
```

- [ ] **Step 2: Run scheduler tests to verify the module is absent**

Run: `uv run pytest tests/test_scheduler.py -v`

Expected: FAIL during collection because `feedsentry.scheduler` does not exist.

- [ ] **Step 3: Implement scheduler tick and lifecycle**

Implement `Scheduler` with injected `ConfigManager`, `Repository`, `IngestionService`, `EventProcessor`, clock, and tick interval. `tick()` performs these operations in order:

1. `config_manager.reload_if_changed()`.
2. Read the current immutable config snapshot.
3. For each enabled monitor and source, call `repository.source_is_due(monitor.id, source_url, now)`.
4. Poll due sources independently; a source error is logged and does not stop the loop.
5. Claim up to 20 due events with `repository.list_due_event_ids(now, limit=20)`.
6. Process each claimed event sequentially for deterministic SQLite behavior.
7. Save `last_tick_at` for the status endpoint.

`run()` loops until an `asyncio.Event` is set and waits with `asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)`. `stop()` sets the event.

- [ ] **Step 4: Wire dependencies in a FastAPI lifespan**

Create `src/feedsentry/app.py` with `create_app(config_path: Path) -> FastAPI`. During lifespan startup:

1. Load initial config.
2. Create and initialize the database.
3. Run `repository.recover_in_progress()`.
4. Create one `httpx.AsyncClient(timeout=20.0, follow_redirects=True)`.
5. Build Feed, AI, Firecrawl, Apprise, ingestion, processor, and scheduler instances.
6. Store this typed dataclass on `app.state.services`:

```python
@dataclass(frozen=True)
class AppServices:
    config_manager: ConfigManager
    repository: Repository
    ingestion: IngestionService
    processor: EventProcessor
    scheduler: Scheduler
    http: httpx.AsyncClient
    database: Database
```
7. Start `scheduler.run()` in an asyncio task.

During shutdown, stop and await the scheduler, close HTTP, and dispose the database. Implement `run()` to read `FEEDSENTRY_CONFIG` with default `config.yaml` and call `uvicorn.run(create_app(path), host="0.0.0.0", port=8000)`.

- [ ] **Step 5: Run scheduler tests and an import smoke test**

Run: `uv run pytest tests/test_scheduler.py -v && uv run python -c "from feedsentry.app import create_app; print(create_app)"`

Expected: scheduler tests pass and the command prints a function object.

- [ ] **Step 6: Commit scheduler and wiring**

```bash
git add src/feedsentry/scheduler.py src/feedsentry/app.py tests/test_scheduler.py tests/conftest.py
git commit -m "feat: run scheduler in application lifespan"
```

## Task 11: Health API and Structured Logging

**Files:**
- Create: `src/feedsentry/logging.py`
- Create: `src/feedsentry/api.py`
- Modify: `src/feedsentry/app.py`
- Modify: `src/feedsentry/processor.py`
- Modify: `src/feedsentry/scheduler.py`
- Create: `tests/test_api.py`
- Create: `tests/test_logging.py`

- [ ] **Step 1: Write API and log-redaction tests**

```python
# tests/test_api.py
from httpx import ASGITransport, AsyncClient


async def test_health_and_status_do_not_expose_secrets(app_fixture) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_fixture.app), base_url="http://test"
    ) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        status = await client.get("/status")
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert status.json()["monitors"] == 1
    assert "secret-ai-key" not in status.text
```

```python
# tests/test_logging.py
import json
import logging

from feedsentry.logging import JsonFormatter


def test_json_formatter_redacts_secrets() -> None:
    record = logging.LogRecord(
        "feedsentry", logging.INFO, __file__, 1, "request api_key=abc token=xyz", (), None
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "abc" not in payload["message"]
    assert "xyz" not in payload["message"]
```

- [ ] **Step 2: Run tests to verify API/logging modules are absent**

Run: `uv run pytest tests/test_api.py tests/test_logging.py -v`

Expected: FAIL during collection because the modules do not exist.

- [ ] **Step 3: Implement JSON logging with bounded secret redaction**

In `src/feedsentry/logging.py`, implement `JsonFormatter` that emits timestamp, level, logger, message, and optional `monitor_id`, `entry_id`, `event_id`, `stage`, and `attempt` record attributes. Replace values following case-insensitive `api_key=`, `token=`, `password=`, and `secret=` patterns with `***`. Truncate the final message to 4000 characters. Add `configure_logging()` that installs this formatter on the root handler.

- [ ] **Step 4: Implement API routes**

```python
# src/feedsentry/api.py
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    services = request.app.state.services
    if services.config_manager.current is None or not await services.repository.ping():
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    services = request.app.state.services
    counts = await services.repository.status_counts()
    return {
        "monitors": len(services.config_manager.current.monitors),
        "last_tick_at": services.scheduler.last_tick_at,
        "pending_events": counts.pending,
        "failed_events": counts.failed,
        "config_error": services.config_manager.last_error,
    }
```

Add `Repository.ping()` and `Repository.status_counts()` with a typed `StatusCounts` dataclass. Include the router and call `configure_logging()` in `create_app()`.

Instrument `EventProcessor` at the beginning of each stage with:

```python
logger.info(
    "processing event stage",
    extra={
        "monitor_id": bundle.event.monitor_id,
        "entry_id": bundle.entry.id,
        "event_id": bundle.event.id,
        "stage": bundle.event.status.value,
        "attempt": bundle.event.failure_count + 1,
    },
)
```

Instrument `Scheduler` before each source poll with `monitor_id` and `stage="collect"`. Log isolated source failures with `logger.exception` and the same structured fields. Never log goals, feed bodies, scraped markdown, model response bodies, or configuration mappings.

- [ ] **Step 5: Run API, logging, and full regression tests**

Run: `uv run pytest tests/test_api.py tests/test_logging.py -v && uv run pytest -q && uv run ruff check .`

Expected: all tests pass and no secret value appears in API responses or formatted logs.

- [ ] **Step 6: Commit health and logging**

```bash
git add src/feedsentry/api.py src/feedsentry/logging.py src/feedsentry/app.py src/feedsentry/processor.py src/feedsentry/scheduler.py src/feedsentry/repository.py tests/test_api.py tests/test_logging.py
git commit -m "feat: add health status and structured logs"
```

## Task 12: End-to-End Proof and Self-Hosted Packaging

**Files:**
- Create: `tests/test_end_to_end.py`
- Modify: `tests/conftest.py`
- Create: `config.example.yaml`
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Write the full success-path and duplicate-suppression test**

```python
# tests/test_end_to_end.py
import httpx
import respx


@respx.mock
async def test_fetch_enrich_summarize_deliver_once(running_app) -> None:
    feed = respx.get("https://example.com/feed.xml")
    feed.side_effect = [
        httpx.Response(200, content=running_app.feed_with("baseline")),
        httpx.Response(200, content=running_app.feed_with("baseline", "new-item")),
        httpx.Response(200, content=running_app.feed_with("baseline", "new-item")),
    ]
    ai = respx.post("http://llm/v1/chat/completions")
    ai.side_effect = [
        running_app.ai_response("fetch", "need release details"),
        running_app.ai_response(
            "accept", "major capability", title="New release", summary="Adds durable workflows"
        ),
    ]
    scrape = respx.post("http://firecrawl:3002/v1/scrape").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"markdown": "Full notes"}})
    )
    notify = respx.post("http://apprise:8000/notify/telegram").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    await running_app.scheduler.tick()
    await running_app.scheduler.tick()
    await running_app.scheduler.tick()

    assert len(scrape.calls) == 1
    assert len(ai.calls) == 2
    assert len(notify.calls) == 1
    assert (await running_app.repository.status_counts()).pending == 0
```

Add a `RunningAppFixture` dataclass and `running_app` fixture to `tests/conftest.py`. The fixture must create a temporary config and SQLite database, construct real `Repository`, `IngestionService`, `EventProcessor`, and `Scheduler` objects with the shared respx-compatible `httpx.AsyncClient`, expose `feed_with(*ids)` that returns deterministic RSS bytes, and expose `ai_response(action, reason, title=None, summary=None)` that returns an `httpx.Response` in OpenAI chat-completions format. Dispose the database and close HTTP after yielding.

- [ ] **Step 2: Run the end-to-end test and fix only integration defects**

Run: `uv run pytest tests/test_end_to_end.py -v`

Expected: PASS with one Firecrawl call, two AI calls, and one Apprise call across repeated scheduler ticks.

- [ ] **Step 3: Add example configuration**

Create `config.example.yaml` matching the validated schema. Use environment references for AI credentials and URLs, leave Firecrawl Key optional, include one disabled example monitor, and document interval syntax inline with YAML comments.

- [ ] **Step 4: Add a minimal non-root container**

```dockerfile
# Dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 feedsentry
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER feedsentry
EXPOSE 8000
ENTRYPOINT ["feedsentry"]
```

```yaml
# compose.yaml
services:
  feedsentry:
    build: .
    restart: unless-stopped
    environment:
      FEEDSENTRY_CONFIG: /config/config.yaml
      AI_BASE_URL: ${AI_BASE_URL}
      AI_API_KEY: ${AI_API_KEY}
      AI_MODEL: ${AI_MODEL}
      FIRECRAWL_BASE_URL: ${FIRECRAWL_BASE_URL}
      FIRECRAWL_API_KEY: ${FIRECRAWL_API_KEY:-}
      APPRISE_BASE_URL: ${APPRISE_BASE_URL}
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - ./data:/app/data
    ports:
      - "8000:8000"
```

- [ ] **Step 5: Document setup, configuration, operation, and failure recovery**

Write `README.md` with exact commands:

```bash
cp config.example.yaml config.yaml
docker compose up --build -d
curl http://localhost:8000/health/ready
curl http://localhost:8000/status
docker compose logs -f feedsentry
```

Explain cold-start baseline behavior, immediate delivery, external RSSHub/Firecrawl/Apprise requirements, optional Firecrawl authentication, environment precedence, SQLite location, four retry delays, and how `FAILED` events remain stored for future CLI/MCP recovery.

- [ ] **Step 6: Ignore runtime artifacts and run final verification**

Ensure `.gitignore` contains `config.yaml`, `data/`, `.env`, `.pytest_cache/`, `.ruff_cache/`, and Python cache files.

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
docker compose config
docker build -t feedsentry:test .
```

Expected: every test passes, Ruff checks and formatting pass, Compose renders successfully, and the image builds as a non-root service.

- [ ] **Step 7: Commit the end-to-end MVP**

```bash
git add .gitignore README.md config.example.yaml Dockerfile compose.yaml tests/test_end_to_end.py
git commit -m "feat: package the FeedSentry MVP"
```

## Final Verification

- [ ] Run `uv run pytest -q` and confirm the entire suite passes.
- [ ] Run `uv run ruff check .` and `uv run ruff format --check .` with no findings.
- [ ] Run `docker compose config` and `docker build -t feedsentry:test .` successfully.
- [ ] Start the container with a test configuration and confirm `/health/live`, `/health/ready`, and `/status` respond without secrets.
- [ ] Feed one synthetic new item through fake or disposable external endpoints and confirm exactly one Apprise notification is received.
- [ ] Restart during `FETCHING` or `DELIVERING`, then confirm the event resumes from that stage without repeating completed AI or Firecrawl work.
