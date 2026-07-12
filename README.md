# FeedSentry

FeedSentry is a self-hosted service that monitors RSS or RSSHub sources, uses an
OpenAI-compatible model to screen new items, optionally enriches relevant items
with Firecrawl, and delivers accepted summaries through Apprise.

```bash
cp config.example.yaml config.yaml
docker compose up --build -d
curl http://localhost:8000/health/ready
curl http://localhost:8000/status
docker compose logs -f feedsentry
```

Set `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, `FIRECRAWL_BASE_URL`,
`FIRECRAWL_API_KEY` (optional), and `APPRISE_BASE_URL` in the environment before
starting Compose. Values in the environment are interpolated into `config.yaml`.

The first successful fetch for each source establishes a cold-start baseline and
sends no notifications. Later new entries are processed immediately using the one
global AI filter and global notification destination configured in `config.yaml`.
FeedSentry requires reachable RSS/RSSHub sources, an OpenAI-compatible API, and
an Apprise API; Firecrawl is optional unless the model chooses enrichment.

SQLite data is stored at the configured `storage.path` (the example uses
`./data/feedsentry.db`). Failed stages retry after 1 minute, 5 minutes, 30
minutes, and 2 hours. After the final retry, failed events remain stored for
future recovery tooling.

FeedSentry intentionally has no task or monitor layer. Add RSS sources under
`sources`, describe what should be accepted under `filter.goal`, and configure one
`destination` for accepted entries. Enabled sources are checked once per minute.
