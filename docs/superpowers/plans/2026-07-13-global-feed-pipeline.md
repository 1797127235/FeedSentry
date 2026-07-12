# Global Feed Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace monitor-scoped processing with one global RSS-to-AI-to-notification pipeline.

**Architecture:** Configuration owns a global filter, source list, and destination. SQLite owns per-source baselines and one durable event per entry; the processor receives the global destination and reads the filter snapshot stored on each event.

**Tech Stack:** Python 3.12, Pydantic, FastAPI, SQLAlchemy asyncio, SQLite, pytest

---

### Task 1: Global configuration

**Files:** `tests/test_config.py`, `src/feedsentry/config.py`, `config.example.yaml`

- [ ] Replace monitor fixtures with `filter`, `sources`, and `destination` tests.
- [ ] Verify focused tests fail because the new models do not exist.
- [ ] Implement `FilterConfig`, `SourceConfig`, and global destination validation.
- [ ] Verify configuration tests pass.

### Task 2: Source- and entry-scoped persistence

**Files:** `tests/test_repository.py`, `src/feedsentry/database.py`, `src/feedsentry/repository.py`

- [ ] Specify source URL primary keys, `events` rows, and one-event-per-entry behavior in tests.
- [ ] Verify repository tests fail against the monitor schema.
- [ ] Replace monitor-keyed tables and repository signatures.
- [ ] Verify repository tests pass, including UTC timestamps and idempotency.

### Task 3: Global ingestion and scheduling

**Files:** `tests/test_ingestion.py`, `tests/test_scheduler.py`, `src/feedsentry/ingestion.py`, `src/feedsentry/scheduler.py`

- [ ] Specify silent per-source baseline and global filter snapshot event creation.
- [ ] Specify polling of enabled sources without task IDs or per-source intervals.
- [ ] Implement the source-only ingestion and one-minute internal poll interval.
- [ ] Verify ingestion and scheduler tests pass.

### Task 4: Global processing and application wiring

**Files:** `tests/test_processor.py`, `tests/test_end_to_end.py`, `src/feedsentry/processor.py`, `src/feedsentry/app.py`, `src/feedsentry/api.py`

- [ ] Specify global destination delivery and the complete new-entry flow.
- [ ] Remove destination resolution by monitor ID.
- [ ] Wire the global destination and update status fields.
- [ ] Verify processor, API, and end-to-end tests pass.

### Task 5: Documentation and verification

**Files:** `README.md`, `docs/architecture.md`, remaining tests

- [ ] Remove monitor/task language and document the new configuration.
- [ ] Run `uv run pytest -q`.
- [ ] Run `uv run ruff check .` and `uv run ruff format --check .`.
- [ ] Run `docker build -t feedsentry:test .` and `docker compose config -q`.

