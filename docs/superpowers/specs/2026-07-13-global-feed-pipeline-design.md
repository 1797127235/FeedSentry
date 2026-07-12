# Global Feed Pipeline Design

## Product model

FeedSentry has one pipeline. It polls every enabled RSS source, sends each newly
discovered entry through one global AI filter, and immediately delivers accepted
entries to one global destination.

There is no monitor or task domain object.

```text
enabled sources -> new entries -> global AI filter -> global destination
```

## Configuration

`config.yaml` remains the only source of operational intent:

```yaml
filter:
  goal: Important product releases
sources:
  - url: https://example.com/feed.xml
    enabled: true
destination:
  kind: apprise
  apprise_key: telegram
```

AI, Firecrawl, Apprise, Telegram, and storage configuration remain global.
Source polling uses an internal one-minute interval. A source may be enabled or
disabled, but does not own filtering, delivery, or scheduling policy.

## Persistence

SQLite stores source state by source URL, entries by source URL and external ID,
one event per entry, scrape cache, and idempotent delivery records. Events retain
the global filter goal used when they were created so retries remain deterministic.

The old monitor-based schema is unsupported. Deployment starts with a new database.
The first successful fetch of every source silently establishes its baseline.

## Processing

Later unseen entries create events immediately. Existing screening, optional
Firecrawl enrichment, retry recovery, prompt-injection protection, and delivery
idempotency remain unchanged. Configuration reload failure retains the last valid
configuration.

## API

The existing health endpoints remain. `/status` reports the number of configured
and enabled sources plus aggregate pending and failed event counts.

