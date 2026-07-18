# FeedSentry Architecture

## Pipeline

FeedSentry has one global processing pipeline:

```text
config.yaml
    -> enabled RSS/RSSHub sources
    -> IngestionService -> FeedClient
    -> Repository -> SQLite
    -> EventProcessor -> AIClient
                      -> FirecrawlClient when feed text is insufficient
                      -> AppriseClient or TelegramNotifier when accepted
```

There is no task or monitor object. All enabled sources use the same
`filter.goal` and the same `destination`.

An authenticated Streamable HTTP MCP adapter exposes control services without moving
business logic into protocol handlers:

```text
Claude/Codex -> /mcp -> Source/Filter/Status/Recovery services
                         -> ConfigStore / RSSHub / Repository / PollCoordinator
```

RSSHub Radar discovery uses the configured instance's `/api/radar/rules`. Direct and
RSSHub sources have stable IDs; an RSSHub source stores its page URL and route while the
runtime Feed URL is derived from the current RSSHub base URL.

## Module layout

```text
feedsentry/
├── app.py          composition root and entry point
├── logging.py      JSON log formatting
├── core/           domain.py, database.py, repository.py
├── config/         models.py (schema + ConfigManager), store.py (atomic YAML writes)
├── clients/        ai.py, feeds.py, feed_validation.py, rsshub.py,
│                   firecrawl.py, apprise.py, telegram.py, qq.py
├── pipeline/       ingestion.py, processor.py, polling.py, scheduler.py
└── interfaces/     api.py, mcp.py, auth.py, control.py, serialize.py
```

Dependencies point downward: `interfaces` -> `pipeline`/`clients` -> `core`/`config`.
`app.py` is the only module that wires all layers together. `tests/` mirrors this
layout.

## Configuration

`config.yaml` is the only source of operational intent. It contains global
integration settings, AI settings, storage, one filter, a source list, and one
destination. `ConfigManager` validates changes before atomically replacing the
in-memory snapshot. Invalid reloads retain the last valid snapshot.

MCP writes go through `ConfigStore`, which locks, rereads the raw YAML, validates a
candidate file, fsyncs a same-directory temporary file, and atomically replaces the
configuration. Raw YAML mutation preserves environment placeholders and prevents
expanded secrets from being written back.

Sources only contain a URL and an enabled flag. Successful sources are checked
once per minute. HTTP failures use 1, 5, 30, and 120 minute backoff delays.

## Cold start and ingestion

`feed_state` is keyed by source URL. The first successful fetch of each source
stores its current entries and establishes a baseline without creating events.
Later unseen entries create one durable event per entry and are eligible for
processing immediately.

Conditional requests use stored ETag and Last-Modified values. A failure for one
source does not block polling other sources.

## Event processing

Events store a snapshot and hash of the global filter goal used at discovery.
This keeps retries deterministic across configuration reloads.

```text
discovered -> screening
screening -> filtered | fetching | delivery_pending
fetching -> summarizing
summarizing -> filtered | delivery_pending
delivery_pending -> delivering -> delivered
external-stage failure -> retry_wait -> failed stage
```

RSS text, fetched Markdown, and model output are untrusted. AI prompts instruct
the model to ignore embedded commands, and Pydantic validates structured model
decisions. Scraped content is cached by article URL.

Feed HTTP responses are size-limited and redirects are checked one hop at a time.
The configured RSSHub private-network exception is scoped to its full origin. These
checks are defense in depth: deployment egress policy must also prevent DNS rebinding
and Firecrawl-side redirects from reaching metadata endpoints or private services.

## Persistence and idempotency

SQLite contains five tables:

- `feed_state`: source validators, baseline, health, next check time, and the
  latest feed title used as the notification source label.
- `entries`: normalized feed entries, unique by source URL and external ID.
- `events`: one state-machine event per entry.
- `scrape_cache`: Firecrawl Markdown keyed by article URL.
- `deliveries`: idempotent delivery records keyed by event and destination.

Repository transitions compare the persisted current status before updating.
Startup recovery returns interrupted external stages to `retry_wait`. SQLite
timestamps are restored as UTC-aware values.

External notification delivery is at-least-once. A process exit after the provider
accepts a message but before the success record commits can result in a duplicate.
Configuration reloads atomically rebind lightweight external clients; `storage.path`
is restart-only because the repository and all state-machine services share one database.

The old monitor-based schema is not migrated. Deployments using this version
must start with a new database; each source then establishes a silent baseline.

## Runtime API

- `GET /health/live`: process liveness.
- `GET /health/ready`: configuration loaded and SQLite reachable.
- `GET /status`: configured/enabled source counts plus pending and failed events.
- `/mcp`: authenticated MCP Streamable HTTP endpoint when `FEEDSENTRY_MCP_TOKEN` is set.

These endpoints do not expose configuration values, feed content, model output,
or secrets.
