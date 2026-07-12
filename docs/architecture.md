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

## Configuration

`config.yaml` is the only source of operational intent. It contains global
integration settings, AI settings, storage, one filter, a source list, and one
destination. `ConfigManager` validates changes before atomically replacing the
in-memory snapshot. Invalid reloads retain the last valid snapshot.

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

## Persistence and idempotency

SQLite contains five tables:

- `feed_state`: source validators, baseline, health, and next check time.
- `entries`: normalized feed entries, unique by source URL and external ID.
- `events`: one state-machine event per entry.
- `scrape_cache`: Firecrawl Markdown keyed by article URL.
- `deliveries`: idempotent delivery records keyed by event and destination.

Repository transitions compare the persisted current status before updating.
Startup recovery returns interrupted external stages to `retry_wait`. SQLite
timestamps are restored as UTC-aware values.

The old monitor-based schema is not migrated. Deployments using this version
must start with a new database; each source then establishes a silent baseline.

## Runtime API

- `GET /health/live`: process liveness.
- `GET /health/ready`: configuration loaded and SQLite reachable.
- `GET /status`: configured/enabled source counts plus pending and failed events.

These endpoints do not expose configuration values, feed content, model output,
or secrets.
