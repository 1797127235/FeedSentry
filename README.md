# FeedSentry

FeedSentry is a self-hosted service that monitors RSS or RSSHub sources, uses an
OpenAI-compatible model to screen new items, optionally enriches relevant items
with Firecrawl, and delivers accepted summaries through Apprise.

```bash
mkdir -p config
cp config.example.yaml config/config.yaml
docker compose up --build -d
curl http://localhost:8000/health/ready
curl http://localhost:8000/status
docker compose logs -f feedsentry
```

Set `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`, `FIRECRAWL_BASE_URL`,
`FIRECRAWL_API_KEY` (optional), and `APPRISE_BASE_URL` in the environment before
starting Compose. Set `FEEDSENTRY_MCP_TOKEN` to enable MCP. Values in the environment
are interpolated into `config.yaml`; the RSSHub base URL is configured directly under
`integrations.rsshub`.

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

## MCP control

FeedSentry can expose authenticated MCP tools for Claude and Codex. Configure RSSHub
and stable source IDs in `config/config.yaml`, then set a random MCP token in `.env`:

```bash
openssl rand -hex 32
```

```dotenv
FEEDSENTRY_MCP_TOKEN=<generated value>
FEEDSENTRY_MCP_ALLOWED_HOSTS=feedsentry.example.com
```

The Streamable HTTP endpoint is `https://feedsentry.example.com/mcp` and clients send
`Authorization: Bearer <token>`. HTTPS is terminated by the reverse proxy. The service
remains bound to localhost; do not expose port 18003 directly.

Available tools discover and subscribe to RSSHub feeds, add direct RSS/Atom feeds,
list/enable/disable/remove sources, manage the global filter, inspect status, force a
source check, recover failed events, and send a marked test notification.

`config/` is mounted read/write because MCP configuration changes use same-directory
temporary files and atomic replacement. Do not mount a single `config.yaml` file: bind
mounting one file prevents reliable atomic replacement in containers.
