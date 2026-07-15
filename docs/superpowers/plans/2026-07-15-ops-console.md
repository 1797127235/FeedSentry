# Ops Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an authenticated REST control API plus a dark React ops console that mirrors MCP tools and adds event audit queries, all through existing Control Services.

**Architecture:** Extend Repository with read-only event listing; expose thin FastAPI `/api/*` routes that call Control Services (same path as MCP). Fix MCP mount so SPA can own `/` while MCP stays at `/mcp`. React+Vite builds into the Docker image and is served by FastAPI when `FEEDSENTRY_MCP_TOKEN` is set.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/aiosqlite, existing control layer; React 18 + Vite + React Router; multi-stage Docker (node:22 + python:3.12-slim).

**Spec:** `docs/superpowers/specs/2026-07-15-ops-console-design.md`

## Global Constraints

- HTTP and MCP call Control Services only — no direct YAML/ORM/Shell from handlers.
- Single global pipeline; no task-level rules or destinations.
- No SQLite schema migration.
- Auth: `FEEDSENTRY_MCP_TOKEN` Bearer; no token → no `/api/*`, no SPA; health and public `/status` remain.
- Never log or return secrets (AI keys, bot tokens, raw notification URLs).
- Preserve MCP URL ` /mcp` for existing clients.
- UTC-aware timestamps from SQLite.
- Tests required for new backend behavior; full pytest + ruff before done.
- Do not commit `config.yaml`, `.env`, or secrets.

## File map

| Path | Responsibility |
| --- | --- |
| `src/feedsentry/auth.py` | Shared Bearer middleware / FastAPI dependency |
| `src/feedsentry/repository.py` | `list_events`, `status_breakdown`, deliveries-for-event |
| `src/feedsentry/control.py` | Event view DTOs; query methods; extended `SystemStatus` |
| `src/feedsentry/api.py` | Public health/status + authenticated `/api` router |
| `src/feedsentry/mcp.py` | Use shared auth; keep tools; path compatible with mount fix |
| `src/feedsentry/app.py` | Wire control services, API router, MCP mount at `/mcp`, SPA static |
| `tests/test_repository.py` | Event list / breakdown tests |
| `tests/test_api_console.py` | Auth + REST happy/error paths |
| `web/` | React+Vite SPA |
| `Dockerfile` | Multi-stage web + app |
| `README.md`, `AGENTS.md` | Enable console + token notes |

### Critical mount fix (do not skip)

Today: `create_mcp_app(..., streamable_http_path="/mcp")` and `app.mount("/", mcp_app)` → public URL `/mcp`.

Target:

1. `create_mcp_app(..., streamable_http_path="/")` (or equivalent so the Starlette app serves MCP at its root).
2. `app.mount("/mcp", mcp_app)` so public URL remains `/mcp`.
3. FastAPI owns `/`, `/api/*`, `/health/*`, `/status`, and SPA assets.

Verify with existing `tests/test_mcp_transport.py` and end-to-end MCP tests after the change.

---

### Task 1: Repository event queries

**Files:**
- Modify: `src/feedsentry/repository.py`
- Test: `tests/test_repository.py`

**Interfaces:**
- Produces:
  - `EventListItem` dataclass (or reuse fields on a new record type)
  - `async def list_events(self, *, status: str | None, source_url: str | None, q: str | None, limit: int, cursor: str | None) -> tuple[list[EventListItem], str | None]`
  - `async def status_breakdown(self) -> dict[str, int]`
  - `async def list_deliveries_for_event(self, event_id: int) -> list[DeliveryRecord]`
  - Cursor encode/decode: opaque string from `(updated_at.isoformat(), id)` ordered `updated_at DESC, id DESC`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_repository.py`:

```python
async def test_list_events_filters_and_paginates(repository: Repository) -> None:
    now = datetime.now(UTC)
    entry_a = await repository.upsert_entry(
        source_url="https://example.com/a.xml",
        external_id="1",
        title="Alpha release",
        summary="summary a",
        link="https://example.com/a/1",
        author=None,
        published_at=now,
        content_hash="h1",
        raw_json="{}",
        first_seen_at=now,
    )
    entry_b = await repository.upsert_entry(
        source_url="https://example.com/b.xml",
        external_id="2",
        title="Beta noise",
        summary="summary b",
        link="https://example.com/b/2",
        author=None,
        published_at=now,
        content_hash="h2",
        raw_json="{}",
        first_seen_at=now,
    )
    # upsert_entry may return entry id or record — match existing test style in this file
    event_a = await repository.create_event(entry_a if isinstance(entry_a, int) else entry_a, "goal", "ghash")
    event_b = await repository.create_event(entry_b if isinstance(entry_b, int) else entry_b, "goal", "ghash")
    await repository.transition_event(
        event_a, EventStatus.DISCOVERED, EventStatus.SCREENING
    )
    await repository.transition_event(
        event_a,
        EventStatus.SCREENING,
        EventStatus.FILTERED,
        decision_reason="not relevant",
    )
    await repository.transition_event(
        event_b, EventStatus.DISCOVERED, EventStatus.SCREENING
    )
    await repository.transition_event(
        event_b,
        EventStatus.SCREENING,
        EventStatus.DELIVERY_PENDING,
        decision_reason="ship it",
        output_title="Beta",
        output_summary="important",
    )

    filtered, cursor = await repository.list_events(
        status=EventStatus.FILTERED.value, source_url=None, q=None, limit=10, cursor=None
    )
    assert len(filtered) == 1
    assert filtered[0].event_id == event_a
    assert filtered[0].decision_reason == "not relevant"

    searched, _ = await repository.list_events(
        status=None, source_url=None, q="Beta", limit=10, cursor=None
    )
    assert [item.event_id for item in searched] == [event_b]

    page1, next_cursor = await repository.list_events(
        status=None, source_url=None, q=None, limit=1, cursor=None
    )
    assert len(page1) == 1
    assert next_cursor is not None
    page2, next2 = await repository.list_events(
        status=None, source_url=None, q=None, limit=1, cursor=next_cursor
    )
    assert len(page2) == 1
    assert page1[0].event_id != page2[0].event_id
    assert next2 is None or page2[0].event_id != page1[0].event_id


async def test_status_breakdown_counts_by_status(repository: Repository) -> None:
    now = datetime.now(UTC)
    entry_id = await repository.upsert_entry(
        source_url="https://example.com/feed.xml",
        external_id="x",
        title="T",
        summary="S",
        link="https://example.com/x",
        author=None,
        published_at=now,
        content_hash="hx",
        raw_json="{}",
        first_seen_at=now,
    )
    if not isinstance(entry_id, int):
        entry_id = entry_id  # adjust if API returns record
    event_id = await repository.create_event(entry_id, "goal", "ghash")
    await repository.transition_event(event_id, EventStatus.DISCOVERED, EventStatus.SCREENING)
    await repository.transition_event(
        event_id, EventStatus.SCREENING, EventStatus.FILTERED, decision_reason="nope"
    )
    breakdown = await repository.status_breakdown()
    assert breakdown.get(EventStatus.FILTERED.value, 0) >= 1
```

**Important:** Before writing tests, read `upsert_entry` / `create_event` signatures and existing tests in `tests/test_repository.py` and match their call patterns exactly (return types differ if helpers wrap ids).

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_repository.py::test_list_events_filters_and_paginates tests/test_repository.py::test_status_breakdown_counts_by_status -v
```

Expected: FAIL (`list_events` / `status_breakdown` missing)

- [ ] **Step 3: Implement repository methods**

Add near other dataclasses in `repository.py`:

```python
@dataclass(frozen=True)
class EventListItem:
    event_id: int
    entry_id: int
    status: EventStatus
    resume_stage: EventStatus | None
    title: str
    link: str
    source_url: str
    decision_reason: str | None
    output_title: str | None
    output_summary: str | None
    failure_count: int
    last_error: str | None
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

Implement:

```python
def _encode_event_cursor(updated_at: datetime, event_id: int) -> str:
    # use base64.urlsafe_b64encode of f"{updated_at.isoformat()}|{event_id}"
    ...

def _decode_event_cursor(cursor: str) -> tuple[datetime, int]:
    # parse; raise ValueError on bad cursor
    ...

async def list_events(...) -> tuple[list[EventListItem], str | None]:
    # SELECT EventRow, EntryRow JOIN
    # WHERE optional status, source_url equality, title LIKE %q%
    # ORDER BY updated_at DESC, id DESC
    # cursor: (updated_at, id) < cursor tuple for keyset pagination
    # fetch limit+1 to compute next_cursor

async def status_breakdown(self) -> dict[str, int]:
    # GROUP BY EventRow.status

async def list_deliveries_for_event(self, event_id: int) -> list[DeliveryRecord]:
    # SELECT deliveries WHERE event_id = ? ORDER BY id
```

Clamp `limit` to 1..100 at repository or control layer (control preferred).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_repository.py::test_list_events_filters_and_paginates tests/test_repository.py::test_status_breakdown_counts_by_status -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/repository.py tests/test_repository.py
git commit -m "feat: add event list and status breakdown queries"
```

---

### Task 2: Control layer views for console

**Files:**
- Modify: `src/feedsentry/control.py`
- Test: `tests/test_control.py` (extend)

**Interfaces:**
- Consumes: `Repository.list_events`, `status_breakdown`, `list_deliveries_for_event`, `get_event_bundle`
- Produces:
  - Extended `SystemStatus` with `last_tick_at: datetime | None` and `status_counts: dict[str, int]`
  - `EventView`, `EventDetailView`, `DeliveryView` dataclasses
  - `StatusService` accepts optional `last_tick_provider: Callable[[], datetime | None]`
  - `StatusService.list_events(...)`, `StatusService.get_event(event_id: int)`
  - Maps `source_url` → `source_id` via current config sources

- [ ] **Step 1: Write failing control tests**

In `tests/test_control.py`, add a focused test using real Repository fixture + fake ConfigManager with one source, create entry/event, call `StatusService.list_events` and assert `source_id` mapping and pagination envelope fields on views.

```python
async def test_status_service_lists_events_with_source_id(repository, tmp_path):
    # build minimal ConfigManager-like object with sources containing id + feed_url
    # StatusService(manager, repository, last_tick_provider=lambda: None)
    # create entry for that feed url + event
    # views, cursor = await service.list_events(status=None, source_id=None, q=None, limit=10, cursor=None)
    # assert views[0].source_id == "example"
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
uv run pytest tests/test_control.py -k list_events -v
```

- [ ] **Step 3: Implement control DTOs and methods**

```python
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
    goal_snapshot: str  # truncate to e.g. 2000 chars if longer
    deliveries: tuple[DeliveryView, ...]
```

Extend `SystemStatus`:

```python
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
```

(`from dataclasses import field`)

`StatusService.__init__(self, manager, repository, last_tick_provider=None)`  
`get_status` fills `last_tick_at` and `status_counts=await repository.status_breakdown()`.

`list_events`: resolve `source_id` filter to feed URL via `source.feed_url(rsshub)`; call repository; map views.

`get_event`: bundle + deliveries; `LookupError` → caller maps to 404.

- [ ] **Step 4: Run control tests PASS**

```bash
uv run pytest tests/test_control.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/control.py tests/test_control.py
git commit -m "feat: expose event audit views on status control service"
```

---

### Task 3: Shared auth + REST `/api` routes

**Files:**
- Create: `src/feedsentry/auth.py`
- Modify: `src/feedsentry/api.py`
- Modify: `src/feedsentry/mcp.py` (use shared Bearer middleware)
- Create: `tests/test_api_console.py`
- Modify: `tests/test_api.py` if public `/status` shape changes

**Interfaces:**
- Produces FastAPI `APIRouter(prefix="/api")` with all read/write routes from the spec
- `require_console_services(request) -> ControlServices` from `app.state.control_services`
- Serialize dataclasses with same rules as MCP `_serialize` (extract small `serialize_public` helper to `auth.py` or `serialize.py` to avoid duplication — prefer moving `_serialize` from mcp to a tiny shared module)

- [ ] **Step 1: Write failing API tests**

`tests/test_api_console.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from feedsentry.api import console_router, public_router  # names as implemented
from feedsentry.mcp import ControlServices  # or feedsentry.control_services


@dataclass
class FakeStatus:
    async def get_status(self):
        return {
            "sources": 1,
            "enabled_sources": 1,
            "pending_events": 0,
            "failed_events": 0,
            "config_error": None,
            "source_statuses": [],
            "last_tick_at": None,
            "status_counts": {"filtered": 1},
        }

    async def list_events(self, **kwargs):
        return [], None

    async def get_event(self, event_id: int):
        raise LookupError(event_id)


def build_app(token: str | None = "secret") -> FastAPI:
    app = FastAPI()
    app.include_router(public_router)
    if token:
        app.include_router(console_router)  # router that depends on token via app.state
        app.state.console_token = token
        app.state.control_services = ControlServices(status=FakeStatus())
    return app


async def test_api_requires_bearer() -> None:
    app = build_app("secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/api/status")
        wrong = await client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        ok = await client.get("/api/status", headers={"Authorization": "Bearer secret"})
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["status_counts"]["filtered"] == 1
```

Add more tests for: `GET /api/events/failed` before id routes (path order), write ops calling fakes (`set_enabled`, `retry_failed_event`), and 404 on missing event.

Implement auth as FastAPI dependency:

```python
async def require_bearer(request: Request) -> None:
    token = getattr(request.app.state, "console_token", None)
    if not token:
        raise HTTPException(404, detail="not found")
    ...
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
uv run pytest tests/test_api_console.py -v
```

- [ ] **Step 3: Implement auth + routes**

`auth.py`:

```python
class BearerTokenMiddleware:  # move from mcp.py
    ...

def verify_bearer(authorization: str | None, token: str) -> bool:
    ...
```

`api.py` structure:

```python
public_router = APIRouter()  # health + /status
console_router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer)])

@console_router.get("/status")
async def api_status(request: Request): ...

@console_router.get("/sources")
...

@console_router.get("/filter")
...

@console_router.get("/events")
...

@console_router.get("/events/failed")  # BEFORE /events/{event_id}
...

@console_router.get("/events/{event_id}")
...

@console_router.post("/feeds/discover")
...

# remaining write routes per spec §4.2
```

Map exceptions:

- `LookupError` / missing source → 404
- `FeedValidationError` / `ValueError` → 400
- control `RuntimeError` for unloaded config → 503

JSON: use serialize helper; datetimes ISO-8601.

- [ ] **Step 4: Run API tests PASS**

```bash
uv run pytest tests/test_api_console.py tests/test_api.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/auth.py src/feedsentry/api.py src/feedsentry/mcp.py tests/test_api_console.py tests/test_api.py
git commit -m "feat: add authenticated REST console API"
```

---

### Task 4: Wire `create_app` (MCP mount + control + SPA hooks)

**Files:**
- Modify: `src/feedsentry/app.py`
- Modify: `src/feedsentry/mcp.py` (`streamable_http_path`)
- Test: `tests/test_mcp_transport.py`, `tests/test_end_to_end.py` (must still pass)
- Optional: small test that without token `/api/status` is 404

- [ ] **Step 1: Write / adjust failing expectations**

If any test assumed mount at `/`, update only if needed. Add:

```python
async def test_console_routes_absent_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("FEEDSENTRY_MCP_TOKEN", raising=False)
    # write minimal config.yaml under tmp_path
    # app = create_app(config_path)
    # AsyncClient GET /api/status → 404
```

- [ ] **Step 2: Run related tests**

```bash
uv run pytest tests/test_mcp_transport.py tests/test_end_to_end.py -v
```

- [ ] **Step 3: Implement app wiring**

In `create_app`:

1. Keep building `control_services = ControlServices()`.
2. If `mcp_token`:
   - `create_mcp_app(..., streamable_http_path="/")`  # public path via mount
   - `app.mount("/mcp", mcp_app)`
3. Always `app.include_router(public_router)`.
4. If `mcp_token`:
   - `app.state.console_token = mcp_token`
   - `app.include_router(console_router)`
5. In lifespan, after creating scheduler:

```python
control_services.status = StatusService(
    config_manager,
    repository,
    last_tick_provider=lambda: scheduler.last_tick_at,
)
app.state.control_services = control_services
```

6. SPA static (if token and dist exists):

```python
web_dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
# in container: /app/web/dist — prefer env FEEDSENTRY_WEB_DIST or Path("/app/web/dist")
if mcp_token and web_dist.is_dir():
    app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")
    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        # do not steal api/mcp/health/status — those already registered
        index = web_dist / "index.html"
        if not index.is_file():
            raise HTTPException(404)
        return FileResponse(index)
```

**Ordering:** register API and health **before** catch-all SPA. Mount `/mcp` before SPA catch-all. Do **not** use `mount("/", mcp_app)`.

7. MCP lifespan: keep `async with mcp_app.router.lifespan_context(mcp_app)` when present.

- [ ] **Step 4: Full MCP + app tests PASS**

```bash
uv run pytest tests/test_mcp_transport.py tests/test_mcp_tools.py tests/test_end_to_end.py tests/test_api_console.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/app.py src/feedsentry/mcp.py tests
git commit -m "feat: wire console API and fix MCP mount for SPA"
```

---

### Task 5: React scaffold, auth, dark shell

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/index.html`
- Create: `web/src/main.tsx`, `web/src/App.tsx`, `web/src/styles.css`, `web/src/api.ts`, `web/src/auth.tsx`
- Create: `web/src/layout/Shell.tsx`, `web/src/pages/LoginPage.tsx`

**Interfaces:**
- `api.ts`: `apiFetch(path, options)` attaches Bearer from `localStorage` key `feedsentry_token`; on 401 clears token and redirects to `/login`
- Vite proxy: `server.proxy["/api"] = "http://127.0.0.1:8000"`

- [ ] **Step 1: Scaffold**

```bash
cd web
npm create vite@latest . -- --template react-ts
# if directory non-empty, create files manually with same layout
npm install react-router-dom
```

`vite.config.ts`:

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/status": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
```

- [ ] **Step 2: Implement token auth + shell**

Dark CSS variables per spec §6.3. Shell: top bar + side nav (概览 / 源 / 事件 / 关注点 / 设置). Login page stores token after successful `GET /api/status`.

- [ ] **Step 3: Manual smoke**

```bash
# terminal A: backend with FEEDSENTRY_MCP_TOKEN set
# terminal B:
cd web && npm run dev
```

Open login, paste token, land on empty dashboard shell.

- [ ] **Step 4: Commit**

```bash
git add web
git commit -m "feat: scaffold React ops console shell and auth"
```

---

### Task 6: Dashboard + sources UI

**Files:**
- Create: `web/src/pages/DashboardPage.tsx`, `SourcesPage.tsx`, `AddSourcePage.tsx`
- Modify: `web/src/api.ts` with typed helpers

- [ ] **Step 1: API helpers**

```ts
export type SystemStatus = { /* fields matching /api/status */ };
export async function getStatus(): Promise<SystemStatus> {
  return apiFetch("/api/status");
}
export async function listSources() { return apiFetch("/api/sources"); }
export async function setSourceEnabled(id: string, enabled: boolean) {
  return apiFetch(`/api/sources/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
}
// check, remove, discover, subscribe, addFeed
```

- [ ] **Step 2: Pages**

- Dashboard: cards for pending/failed/enabled sources/`last_tick_at`; table of sources with `consecutive_failures > 0` or `last_error`; Refresh button.
- Sources: full table; toggles; Check Now; Delete with `confirm()`.
- Add source: two tabs — direct URL POST `/api/feeds`; page URL discover → list candidates → subscribe.

- [ ] **Step 3: Manual verify against running API**

- [ ] **Step 4: Commit**

```bash
git add web
git commit -m "feat: add dashboard and source management pages"
```

---

### Task 7: Events, filter, settings pages

**Files:**
- Create: `web/src/pages/EventsPage.tsx`, `EventDetailPage.tsx`, `FilterPage.tsx`, `SettingsPage.tsx`

- [ ] **Step 1: Events list**

Query params: status tabs (`"" | filtered | delivered | failed | ...`), optional source_id, q, cursor load-more.

- [ ] **Step 2: Event detail**

Show decision_reason, output_*, goal_snapshot, deliveries; Retry button if status failed → POST `/api/events/{id}/retry`.

- [ ] **Step 3: Filter + settings**

- Filter: GET/PUT goal; helper text: 仅影响之后新条目.
- Settings: change/clear token; POST `/api/destination/test` with success toast.

- [ ] **Step 4: Commit**

```bash
git add web
git commit -m "feat: add events audit, filter, and settings pages"
```

---

### Task 8: Docker multi-stage + docs

**Files:**
- Modify: `Dockerfile`
- Modify: `README.md`, `AGENTS.md`
- Optionally: `.dockerignore` to exclude `web/node_modules`

- [ ] **Step 1: Dockerfile**

```dockerfile
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS builder
# existing uv sync steps...

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 feedsentry
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=web /web/dist /app/web/dist
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
ENV FEEDSENTRY_WEB_DIST=/app/web/dist
USER feedsentry
EXPOSE 8000
ENTRYPOINT ["feedsentry"]
```

Ensure `app.py` resolves web dist from `FEEDSENTRY_WEB_DIST` then fallbacks.

- [ ] **Step 2: Generate lockfile if missing**

```bash
cd web && npm install && npm run build
```

- [ ] **Step 3: Docs**

README section: 启用控制台 = set `FEEDSENTRY_MCP_TOKEN`; open `https://.../`; Bearer same as MCP; reverse proxy must pass `Authorization` for `/api` too.

AGENTS.md: note console is control-plane only via REST; no second pipeline.

- [ ] **Step 4: Build**

```bash
docker build -t feedsentry:test .
docker compose config -q
```

Expected: build succeeds.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile web/package-lock.json README.md AGENTS.md src/feedsentry/app.py .dockerignore
git commit -m "feat: ship ops console in multi-stage Docker image"
```

---

### Task 9: Full verification

- [ ] **Step 1: Backend suite**

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Expected: all pass.

- [ ] **Step 2: Frontend production build**

```bash
cd web && npm run build
```

- [ ] **Step 3: Docker build**

```bash
docker build -t feedsentry:test .
```

- [ ] **Step 4: Manual checklist (local or container with token)**

1. No token → `/api/status` 404, `/health/live` 200, pipeline starts.
2. With token → login works; dashboard shows counts.
3. Add direct feed; list sources; disable; check now.
4. Events page lists statuses; open detail; failed retry if fixture available.
5. Change filter goal; test destination (marks TEST).
6. MCP still lists 13 tools at `/mcp`.

- [ ] **Step 5: Final commit if any fixes**

```bash
git add -A
git status  # ensure no secrets
git commit -m "test: finish ops console verification fixes"
```

---

## Spec coverage checklist

| Spec section | Task(s) |
| --- | --- |
| REST read APIs + event audit | 1, 2, 3 |
| REST write = MCP tools | 3, 4 |
| Auth / no token disables console | 3, 4 |
| React pages + dark UI | 5–7 |
| Multi-stage Docker + single service | 8 |
| Tests + success criteria | 1–4, 9 |
| MCP URL preserved | 4 |
| No schema migration / control-only writes | 1–3 |

## Self-review notes

- No TBD placeholders left in tasks.
- `upsert_entry` return type must be confirmed from code when implementing Task 1 tests.
- MCP mount change is load-bearing; run MCP tests immediately after Task 4.
- SPA catch-all must not override `/api`, `/mcp`, `/health`, `/status`.
- Unrelated dirty files (`ai.py`, `.firecrawl/`) must not be committed with this work.
