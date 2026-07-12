# FeedSentry MCP Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an authenticated remote MCP control plane that lets Claude and Codex discover and subscribe to RSSHub feeds, manage direct feeds and the global filter, inspect health, trigger polling, and recover failed events without bypassing FeedSentry's durable state machine.

**Architecture:** New control services own configuration mutation and operational commands. A thin MCP adapter exposes those services over Streamable HTTP with Bearer authentication; RSSHub Radar discovery, feed validation, polling coordination, and repository recovery remain independently testable modules.

**Tech Stack:** Python 3.12+, FastAPI/Starlette ASGI, official MCP Python SDK, Pydantic 2, httpx, feedparser, SQLAlchemy asyncio, SQLite, PyYAML, pytest/respx

---

## File map

- `src/feedsentry/config.py`: discriminated direct/RSSHub source models and RSSHub integration config.
- `src/feedsentry/config_store.py`: locked, validated, atomic YAML field updates that preserve environment placeholders.
- `src/feedsentry/rsshub.py`: Radar rules client, matcher, cache, and signed candidate codec.
- `src/feedsentry/feed_validation.py`: bounded RSS/Atom fetch and validation.
- `src/feedsentry/polling.py`: per-source locks shared by scheduler and immediate checks.
- `src/feedsentry/control.py`: source, filter, status, recovery, and destination control services.
- `src/feedsentry/mcp.py`: tool declarations, result schemas, auth middleware, and MCP ASGI lifecycle.
- `src/feedsentry/database.py`, `repository.py`: source metadata/status queries and failed-stage recovery.
- `src/feedsentry/app.py`, `scheduler.py`, `ingestion.py`: resolve configured sources and wire shared services.
- `tests/test_*.py`: module-aligned unit and integration coverage.

### Task 1: Prove the official MCP transport integration

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/feedsentry/mcp.py`
- Create: `tests/test_mcp_transport.py`

- [ ] **Step 1: Add the official SDK dependency**

Run:

```bash
uv add "mcp>=1,<2"
```

Expected: `pyproject.toml` and `uv.lock` contain the official `mcp` package without unrelated upgrades.

- [ ] **Step 2: Write a failing real-client transport test**

Create `tests/test_mcp_transport.py` using `mcp.ClientSession` and
`mcp.client.streamable_http.streamablehttp_client`. Start the candidate ASGI app on an ephemeral localhost port, initialize a session against `/mcp`, and assert `list_tools()` returns a temporary `get_status` tool. The fixture must start and stop the ASGI lifespan so the SDK's Streamable HTTP task group is initialized.

```python
async def test_streamable_http_initializes_and_lists_tools(mcp_server_url: str) -> None:
    async with streamablehttp_client(f"{mcp_server_url}/mcp") as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert [tool.name for tool in tools.tools] == ["get_status"]
```

- [ ] **Step 3: Run the test and verify RED**

Run:

```bash
uv run pytest -q tests/test_mcp_transport.py
```

Expected: FAIL because `feedsentry.mcp` and its ASGI factory do not exist.

- [ ] **Step 4: Implement the smallest MCP ASGI factory**

Implement `create_mcp_app()` with the installed SDK's current API. Set the SDK's internal Streamable HTTP path so the externally mounted endpoint is exactly `/mcp`, and expose only a temporary `get_status` tool returning `{"status": "ok"}`. Compose the SDK lifespan with the host app rather than starting a second server.

- [ ] **Step 5: Verify GREEN and record the proven mount contract**

Run:

```bash
uv run pytest -q tests/test_mcp_transport.py
```

Expected: PASS with a real MCP initialize and tool-list exchange at `/mcp`, with no redirect or nested `/mcp/mcp` path.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/feedsentry/mcp.py tests/test_mcp_transport.py
git commit -m "feat: establish MCP streamable HTTP transport"
```

### Task 2: Model RSSHub and stable sources

**Files:**
- Modify: `src/feedsentry/config.py`
- Modify: `config.example.yaml`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing configuration tests**

Specify this public shape:

```yaml
integrations:
  rsshub:
    base_url: https://rsshub.antest.cc.cd
sources:
  - id: v2ex
    kind: feed
    url: https://www.v2ex.com/index.xml
    enabled: true
  - id: bilibili-video
    kind: rsshub
    page_url: https://space.bilibili.com/946974
    route: /bilibili/user/video/946974
    enabled: true
```

Tests must reject duplicate IDs, duplicate resolved URLs, IDs outside
`^[a-z0-9][a-z0-9-]*$`, direct sources without `url`, RSSHub sources without
`page_url`/absolute-path `route`, and RSSHub sources without the integration.
Assert `source.feed_url(config.integrations.rsshub)` resolves both variants.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/test_config.py
```

Expected: FAIL because `RSSHubConfig` and discriminated source variants do not exist.

- [ ] **Step 3: Implement the source models**

Add `RSSHubConfig`, `DirectSourceConfig`, and `RSSHubSourceConfig`, with a
discriminated `SourceConfig` union keyed by `kind`. Normalize the RSSHub base URL
without embedding credentials and resolve routes with URL path joining, never raw string concatenation.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest -q tests/test_config.py
```

Expected: all configuration tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/config.py config.example.yaml tests/test_config.py
git commit -m "feat: model direct and RSSHub sources"
```

### Task 3: Implement RSSHub Radar discovery

**Files:**
- Create: `src/feedsentry/rsshub.py`
- Create: `tests/test_rsshub.py`

- [ ] **Step 1: Save bounded real rule fixtures**

In `tests/test_rsshub.py`, define minimal rules matching the production schema:

```python
RULES = {
    "bilibili.com": {
        "_name": "哔哩哔哩",
        "space": [
            {
                "title": "UP 主视频",
                "source": ["/:uid"],
                "target": "/bilibili/user/video/:uid",
            },
            {
                "title": "UP 主动态",
                "source": ["/:uid"],
                "target": "/bilibili/user/dynamic/:uid",
            },
        ],
    }
}
```

- [ ] **Step 2: Write failing matcher and client tests**

Cover domain normalization (`www.` and subdomains), source path matching, percent-safe parameter substitution, multiple candidates, no match, `GET /api/radar/rules`, TTL cache reuse, stale-on-refresh-error, and no-cache failure. Add signed candidate tests for round trip, tampering, and expiry.

- [ ] **Step 3: Verify RED**

```bash
uv run pytest -q tests/test_rsshub.py
```

Expected: FAIL because `RSSHubClient`, `RadarMatcher`, and `CandidateCodec` do not exist.

- [ ] **Step 4: Implement discovery**

Create immutable `FeedCandidate` with `title`, `page_url`, `route`, and `feed_url`.
`RSSHubClient.rules()` fetches `/api/radar/rules` with timeout and a response-size
limit. `RadarMatcher.discover(page_url, rules, base_url)` returns deterministic,
deduplicated candidates. `CandidateCodec` signs compact JSON using HMAC-SHA256 and
`FEEDSENTRY_MCP_TOKEN`, verifies with `hmac.compare_digest`, and enforces expiry.

- [ ] **Step 5: Verify GREEN**

```bash
uv run pytest -q tests/test_rsshub.py
```

Expected: all Radar and candidate tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/feedsentry/rsshub.py tests/test_rsshub.py
git commit -m "feat: discover feeds through RSSHub Radar"
```

### Task 4: Validate feeds safely

**Files:**
- Create: `src/feedsentry/feed_validation.py`
- Create: `tests/test_feed_validation.py`
- Modify: `src/feedsentry/feeds.py`

- [ ] **Step 1: Write failing validation tests**

Cover RSS 2.0, Atom 1.0, a valid empty feed, HTML returned with status 200, malformed XML, HTTP failure, redirect to a forbidden address, oversized response, and a configured RSSHub private host exception. Assert the result includes canonical URL, feed title, feed version, validators, and normalized entries.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/test_feed_validation.py
```

Expected: FAIL because `FeedValidator` does not exist.

- [ ] **Step 3: Implement the validator**

Use `httpx.AsyncClient.stream()` and count bytes before parsing. Accept only HTTP(S),
resolve DNS and reject loopback/link-local/private targets unless the hostname is the
explicit configured RSSHub host, and repeat checks after redirects. Parse once with
`feedparser`; require a recognized RSS/Atom `version`, but allow zero entries.
Refactor `feeds.py` so normalization can consume an already-parsed feed without a
second parse.

- [ ] **Step 4: Verify GREEN and feed regressions**

```bash
uv run pytest -q tests/test_feed_validation.py tests/test_feeds.py
```

Expected: all validation and existing normalization tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/feed_validation.py src/feedsentry/feeds.py tests/test_feed_validation.py tests/test_feeds.py
git commit -m "feat: validate feed sources safely"
```

### Task 5: Add atomic configuration mutation

**Files:**
- Create: `src/feedsentry/config_store.py`
- Create: `tests/test_config_store.py`
- Modify: `src/feedsentry/config.py`

- [ ] **Step 1: Write failing mutation tests**

Cover add, idempotent add, enable/disable, remove, and filter replacement. Assert
`${AI_API_KEY}` and other environment placeholders remain literal in YAML after every
write. Simulate validation failure and `os.replace` failure and assert original bytes
are unchanged. Run two async mutations concurrently and assert both changes survive.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/test_config_store.py
```

Expected: FAIL because `ConfigStore` does not exist.

- [ ] **Step 3: Implement raw-YAML atomic updates**

`ConfigStore` owns an `asyncio.Lock`. Inside the lock it rereads YAML, mutates only
`sources` or `filter.goal`, validates through a non-secret-leaking `validate_mapping`
helper, writes a same-directory `NamedTemporaryFile`, flushes and `os.fsync()`s it,
then uses `os.replace()`. After replacement, call `ConfigManager.load_initial()` so
the successful writer observes the exact committed file.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest -q tests/test_config_store.py tests/test_config.py
```

Expected: all config store and existing reload tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/config_store.py src/feedsentry/config.py tests/test_config_store.py tests/test_config.py
git commit -m "feat: add atomic configuration store"
```

### Task 6: Coordinate polling and source resolution

**Files:**
- Create: `src/feedsentry/polling.py`
- Create: `tests/test_polling.py`
- Modify: `src/feedsentry/scheduler.py`
- Modify: `src/feedsentry/ingestion.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_ingestion.py`

- [ ] **Step 1: Write failing coordination tests**

Specify `PollCoordinator.poll(source, goal, force=False)`. Assert concurrent scheduler
and forced calls for one source execute ingestion once at a time, different sources
can run concurrently, disabled sources are rejected for normal polls, and `force=True`
ignores `next_check_at` without altering cold-start behavior.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/test_polling.py tests/test_scheduler.py
```

Expected: FAIL because `PollCoordinator` does not exist.

- [ ] **Step 3: Implement and wire the coordinator**

Resolve each configured source to its current feed URL before querying repository state.
Move per-source due checking and locking into `PollCoordinator`; have Scheduler and the
future control service call the same object. Keep `IngestionService` responsible only
for one resolved Feed URL and its goal snapshot.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest -q tests/test_polling.py tests/test_scheduler.py tests/test_ingestion.py
```

Expected: coordination, scheduling, and silent baseline tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/polling.py src/feedsentry/scheduler.py src/feedsentry/ingestion.py tests/test_polling.py tests/test_scheduler.py tests/test_ingestion.py
git commit -m "feat: coordinate scheduled and immediate source polls"
```

### Task 7: Build source and filter control services

**Files:**
- Create: `src/feedsentry/control.py`
- Create: `tests/test_control.py`
- Modify: `src/feedsentry/repository.py`

- [ ] **Step 1: Write failing source-control tests**

Cover `discover_feeds`, direct add, candidate subscribe, duplicate add, list with
`FeedStateRecord`, enable/disable, remove, and immediate check. Assert add persists the
validated entries and marks initialization without events. Assert config-success/
baseline-failure returns `baseline_pending` and remains safe on the scheduler's next poll.

- [ ] **Step 2: Write failing filter and status tests**

Cover get/set goal, aggregate status, per-source failure details, and absent feed state.
Use result dataclasses/Pydantic models rather than returning ORM rows or raw YAML.

- [ ] **Step 3: Verify RED**

```bash
uv run pytest -q tests/test_control.py
```

Expected: FAIL because control services and repository list queries do not exist.

- [ ] **Step 4: Implement control services**

Create `SourceService`, `FilterService`, and `StatusService`. Add repository queries for
batched feed states. Source IDs are generated from validated titles/hosts with a stable
hash suffix on collision. All writes delegate to `ConfigStore`; all checks delegate to
`FeedValidator`/`PollCoordinator`.

- [ ] **Step 5: Verify GREEN**

```bash
uv run pytest -q tests/test_control.py tests/test_repository.py
```

Expected: source, filter, status, and repository tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/feedsentry/control.py src/feedsentry/repository.py tests/test_control.py tests/test_repository.py
git commit -m "feat: add FeedSentry control services"
```

### Task 8: Preserve and recover failed stages

**Files:**
- Modify: `src/feedsentry/repository.py`
- Modify: `tests/test_repository.py`
- Modify: `tests/test_processor.py`
- Modify: `src/feedsentry/control.py`
- Modify: `tests/test_control.py`

- [ ] **Step 1: Write failing terminal-failure tests**

Drive screening, fetching, summarizing, and delivering through five failures. Assert the
event is `failed`, `resume_stage` retains the exact failed stage, and
`RecoveryService.retry_failed_event()` moves it to `retry_wait` due now while keeping the
stage. Assert non-failed and unknown events are rejected without mutation.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/test_repository.py tests/test_processor.py tests/test_control.py
```

Expected: FAIL because terminal failure currently clears `resume_stage` and no recovery service exists.

- [ ] **Step 3: Implement protected recovery**

Keep `resume_stage=failed_stage.value` when setting `failed`. Add a compare-and-set
repository operation accepting only `failed` with a valid resumable stage and setting
`retry_wait`, `next_attempt_at=now`, and preserving completed outputs/caches/deliveries.
Expose it through `RecoveryService` and add `list_failed_events()` with bounded errors.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest -q tests/test_repository.py tests/test_processor.py tests/test_control.py
```

Expected: all failure-stage and recovery tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/repository.py src/feedsentry/control.py tests/test_repository.py tests/test_processor.py tests/test_control.py
git commit -m "feat: recover terminal events from their failed stage"
```

### Task 9: Add destination testing

**Files:**
- Modify: `src/feedsentry/control.py`
- Modify: `tests/test_control.py`
- Modify: `src/feedsentry/app.py`

- [ ] **Step 1: Write failing tests**

Test Apprise and native Telegram destinations. Assert both title and body contain
`FeedSentry TEST`, the configured current destination is used after hot reload, success
returns a bounded response summary, and client failures return a sanitized control error.

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/test_control.py -k destination
```

Expected: FAIL because `DestinationService` does not exist.

- [ ] **Step 3: Implement `DestinationService.test()`**

Reuse injected `AppriseClient` and `TelegramNotifier`. Do not create an event or delivery
record for this explicit test notification; emit an audit log with operation and result.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest -q tests/test_control.py -k destination
```

Expected: all destination tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/feedsentry/control.py src/feedsentry/app.py tests/test_control.py
git commit -m "feat: add controlled destination testing"
```

### Task 10: Expose authenticated MCP tools

**Files:**
- Modify: `src/feedsentry/mcp.py`
- Modify: `src/feedsentry/app.py`
- Modify: `tests/test_mcp_transport.py`
- Create: `tests/test_mcp_tools.py`

- [ ] **Step 1: Write failing authentication tests**

Against the real ASGI endpoint, assert missing, malformed, and wrong Bearer tokens return
401 without initializing MCP; the correct token succeeds. Assert responses and logs never
contain the configured token. When `FEEDSENTRY_MCP_TOKEN` is absent, `/mcp` returns 404
while health endpoints remain functional.

- [ ] **Step 2: Write failing tool-contract tests**

Using the official MCP client, list and call exactly these tools:

```text
discover_feeds
subscribe_feed
add_feed
list_sources
set_source_enabled
remove_source
check_source_now
get_filter_goal
set_filter_goal
get_status
list_failed_events
retry_failed_event
test_destination
```

Assert JSON schemas require the documented arguments and calls return structured content
from fake control services. Assert domain errors map to stable error codes without stack
traces or secrets.

- [ ] **Step 3: Verify RED**

```bash
uv run pytest -q tests/test_mcp_transport.py tests/test_mcp_tools.py
```

Expected: FAIL because the temporary tool and unauthenticated app do not meet the contract.

- [ ] **Step 4: Implement thin tools and auth middleware**

Build tools from injected control services. Apply Bearer authentication before the SDK
ASGI app using `secrets.compare_digest`; cap request body size and concurrent MCP requests.
Compose the MCP lifespan into `create_app()` and expose exactly `/mcp`.

- [ ] **Step 5: Verify GREEN**

```bash
uv run pytest -q tests/test_mcp_transport.py tests/test_mcp_tools.py tests/test_api.py
```

Expected: transport, auth, tool schemas/calls, and existing health APIs pass.

- [ ] **Step 6: Commit**

```bash
git add src/feedsentry/mcp.py src/feedsentry/app.py tests/test_mcp_transport.py tests/test_mcp_tools.py tests/test_api.py
git commit -m "feat: expose authenticated FeedSentry MCP tools"
```

### Task 11: Update deployment and documentation

**Files:**
- Modify: `compose.yaml`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `AGENTS.md`
- Modify: `.gitignore`

- [ ] **Step 1: Update deployment configuration**

Add `FEEDSENTRY_MCP_TOKEN` to Compose, remove `:ro` from the `config.yaml` bind mount,
and keep the service bound to `127.0.0.1:18003`. Document that the reverse proxy publishes
only HTTPS `/mcp`; do not commit a domain, token, `.env`, or real `config.yaml`.

- [ ] **Step 2: Document configuration and client results**

Show `integrations.rsshub.base_url: https://rsshub.antest.cc.cd`, direct/RSSHub source
examples, token generation with `openssl rand -hex 32`, the MCP URL/header contract, tool
list, cold-start behavior, database rebuild requirement, and safe config write semantics.

- [ ] **Step 3: Verify no secrets or local artifacts are tracked**

```bash
git status --short --untracked-files=all
git grep -n "FEEDSENTRY_MCP_TOKEN=" -- ':!docs/superpowers/**'
git check-ignore config.yaml .env 'config.yaml.before-test'
```

Expected: only intended project changes; no token value; all secret/local files ignored.

- [ ] **Step 4: Commit**

```bash
git add compose.yaml README.md docs/architecture.md AGENTS.md .gitignore
git commit -m "docs: describe MCP control plane deployment"
```

### Task 12: End-to-end and release verification

**Files:**
- Modify: `tests/test_end_to_end.py`

- [ ] **Step 1: Write an end-to-end MCP subscription test**

Run a real MCP client against the ASGI app with fake RSSHub, feed, AI, and destination
HTTP services. Execute `discover_feeds`, choose the video candidate, call
`subscribe_feed`, assert existing entries create no events, publish one new item, force a
poll, and assert exactly one AI decision and one notification.

- [ ] **Step 2: Verify RED, then implement only missing wiring**

```bash
uv run pytest -q tests/test_end_to_end.py -k mcp
```

Expected before final wiring: FAIL at the first missing integration boundary. Add only
the dependency wiring needed to complete the approved flow.

- [ ] **Step 3: Run focused GREEN verification**

```bash
uv run pytest -q tests/test_end_to_end.py -k mcp
```

Expected: the complete platform-page-to-new-item-notification flow passes.

- [ ] **Step 4: Run the complete local quality gate**

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Run container verification**

```bash
docker build -t feedsentry:test .
docker compose config -q
```

Expected: image builds and Compose validates without printing secrets.

- [ ] **Step 6: Verify a disposable runtime**

Start with a disposable database and controlled Feed, then verify:

```bash
curl -fsS http://127.0.0.1:18003/health/live
curl -fsS http://127.0.0.1:18003/health/ready
curl -fsS http://127.0.0.1:18003/status
```

Use an official MCP client to initialize over HTTPS with the Bearer token, list tools,
discover a controlled Radar candidate, subscribe it, and verify baseline silence followed
by exactly one notification for a controlled new item.

- [ ] **Step 7: Commit final test wiring**

```bash
git add tests/test_end_to_end.py src/feedsentry
git commit -m "test: cover MCP subscription workflow end to end"
```
