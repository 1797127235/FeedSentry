# FeedSentry Agent Guide

## Project Purpose

FeedSentry is a self-hosted Python service that polls RSS/RSSHub feeds, filters
new entries through an OpenAI-compatible model, optionally enriches content with
Firecrawl, and sends accepted summaries through Apprise.

The service is designed around a durable SQLite event state machine. Do not
replace persisted state with in-memory queues or bypass idempotency checks.

## Local Development

Use `uv` from the repository root:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

New behavior requires tests. Run the focused test first, then the complete
suite before committing. Keep `src/feedsentry/` and `tests/` aligned by module.

## Important Architecture Rules

- `config.yaml` is the source of truth for monitor definitions; SQLite stores
  baselines, entries, event state, scrape cache, and delivery attempts.
- The first successful poll for each `monitor_id` plus source URL creates a
  baseline only. It must not notify for historical entries.
- Preserve the event state machine and guarded repository transitions. A retry
  must resume its failed stage without repeating completed AI, Firecrawl, or
  Apprise work.
- Delivery rows are idempotent per event and Apprise destination.
- Keep all external clients async and injectable for deterministic tests.
- Treat RSS text, scraped content, and model output as untrusted input. Do not
  remove the AI prompt-injection safeguards.
- SQLite timestamps must remain UTC-aware when read from the repository.

## Configuration And Secrets

- Never commit `config.yaml`, `.env`, API keys, bot tokens, notification URLs,
  database files, or runtime logs.
- `config.example.yaml` documents the required environment variables.
- `AI_BASE_URL` is a base URL ending in `/v1`; the application appends
  `/chat/completions`.
- Firecrawl and Apprise run on the deployment host. Containers access them via
  `host.docker.internal`; Compose provides that host gateway mapping.
- Apprise destinations are configured in Apprise itself. FeedSentry references
  a destination through `destination.apprise_key` only.

## Production Deployment

The active server deployment is intentionally local-only:

```text
Host: 38.246.112.19
Directory: /home/anya/feedsentry
Compose service: feedsentry
Host port: 127.0.0.1:18003 -> container port 8000
Data: /home/anya/feedsentry/data/feedsentry.db
```

Manage it over SSH:

```bash
ssh 38.246.112.19
cd /home/anya/feedsentry
docker compose up -d --build
docker compose ps
docker compose logs -f feedsentry
curl http://127.0.0.1:18003/health/ready
curl http://127.0.0.1:18003/status
```

The container runs as UID 10001. The host `data/` directory must grant that UID
write access; do not solve permission failures by running the service as root.

Before changing production configuration, validate Compose without printing
secrets:

```bash
docker compose config -q
```

## Verification Expectations

- Local: full pytest suite, Ruff lint, and Ruff format check.
- Container: `docker build -t feedsentry:test .` and `docker compose config -q`.
- Runtime: check `/health/live`, `/health/ready`, and `/status`.
- A new source starts with a silent baseline. Use a disposable feed or a known
  new entry for delivery tests.
- Before a real notification test, confirm the intended Apprise configuration
  key and label the message as a test.

## Change Scope

Keep changes narrow. Avoid unrelated dependency upgrades, schema rewrites, and
formatting churn. If a schema migration is needed, provide an explicit upgrade
path that preserves existing SQLite data.
