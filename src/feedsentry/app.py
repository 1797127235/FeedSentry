from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from feedsentry.clients.ai import AIClient
from feedsentry.clients.apprise import AppriseClient
from feedsentry.clients.feed_validation import FeedValidator
from feedsentry.clients.feeds import FeedClient
from feedsentry.clients.firecrawl import FirecrawlClient
from feedsentry.clients.qq import QQNotifier
from feedsentry.clients.rsshub import CandidateCodec, RSSHubClient
from feedsentry.clients.telegram import TelegramNotifier
from feedsentry.config.models import ConfigManager
from feedsentry.config.store import ConfigStore
from feedsentry.core.database import Database, create_database
from feedsentry.core.repository import Repository
from feedsentry.interfaces.api import console_router, public_router
from feedsentry.interfaces.control import (
    DestinationService,
    FilterService,
    RecoveryService,
    SourceService,
    StatusService,
)
from feedsentry.interfaces.mcp import ControlServices, create_mcp_app
from feedsentry.logging import configure_logging
from feedsentry.pipeline.ingestion import IngestionService
from feedsentry.pipeline.polling import PollCoordinator
from feedsentry.pipeline.processor import EventProcessor
from feedsentry.pipeline.scheduler import Scheduler


@dataclass(frozen=True)
class AppServices:
    config_manager: ConfigManager
    repository: Repository
    ingestion: IngestionService
    polling: PollCoordinator
    processor: EventProcessor
    scheduler: Scheduler
    http: httpx.AsyncClient
    database: Database


def _resolve_web_dist() -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("FEEDSENTRY_WEB_DIST")
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/app/web/dist"))
    candidates.append(Path(__file__).resolve().parent.parent.parent / "web" / "dist")
    for path in candidates:
        if path.is_dir():
            return path
    return None


def create_app(config_path: Path) -> FastAPI:
    configure_logging()
    mcp_token = os.environ.get("FEEDSENTRY_MCP_TOKEN")
    control_services = ControlServices()
    allowed_hosts = [
        item.strip()
        for item in os.environ.get(
            "FEEDSENTRY_MCP_ALLOWED_HOSTS",
            "localhost,localhost:*,127.0.0.1,127.0.0.1:*",
        ).split(",")
        if item.strip()
    ]
    mcp_app = (
        create_mcp_app(control_services, token=mcp_token, allowed_hosts=allowed_hosts)
        if mcp_token
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config_manager = ConfigManager(config_path)
        config = config_manager.load_initial()
        database = create_database(config.storage.path)
        await database.initialize()
        repository = Repository(database.session_factory)
        await repository.recover_in_progress()
        http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        feed_client = FeedClient(http)
        ai_client = AIClient(http, str(config.ai.base_url), config.ai.api_key, config.ai.model)
        firecrawl_client = FirecrawlClient(
            http, str(config.integrations.firecrawl.base_url), config.integrations.firecrawl.api_key
        )
        apprise_client = AppriseClient(http, str(config.integrations.apprise.base_url))
        telegram_client = (
            TelegramNotifier(
                http, config.integrations.telegram.bot_token, config.integrations.telegram.chat_id
            )
            if config.integrations.telegram is not None
            else None
        )
        qq_config = config.integrations.qq
        qq_client = (
            QQNotifier(
                http,
                str(qq_config.base_url),
                qq_config.access_token,
                qq_config.target_type,
                qq_config.target_id,
            )
            if qq_config is not None
            else None
        )
        ingestion = IngestionService(repository, feed_client)
        polling = PollCoordinator(repository, ingestion)
        config_store = ConfigStore(config_manager)
        rsshub_base_url = (
            str(config.integrations.rsshub.base_url)
            if config.integrations.rsshub is not None
            else "http://rsshub.invalid"
        )
        rsshub_client = RSSHubClient(http, rsshub_base_url)
        allowed_private_hosts = (
            {config.integrations.rsshub.base_url.host}
            if config.integrations.rsshub is not None
            and config.integrations.rsshub.base_url.host is not None
            else set()
        )
        feed_validator = FeedValidator(http, allowed_private_hosts=allowed_private_hosts)

        def current_destination():
            current = config_manager.current
            if current is None:
                raise RuntimeError("configuration is not loaded")
            return current.destination

        processor = EventProcessor(
            repository,
            ai_client,
            firecrawl_client,
            apprise_client,
            current_destination,
            telegram_client,
            qq_client,
        )
        scheduler = Scheduler(config_manager, repository, polling, processor)
        control_services.sources = SourceService(
            config_manager,
            config_store,
            repository,
            feed_validator,
            rsshub_client,
            CandidateCodec(mcp_token.encode() if mcp_token else b"disabled"),
            polling,
        )
        control_services.filter = FilterService(config_manager, config_store)
        control_services.status = StatusService(
            config_manager,
            repository,
            last_tick_provider=lambda: scheduler.last_tick_at,
        )
        control_services.recovery = RecoveryService(repository)
        control_services.destination = DestinationService(
            config_manager, apprise_client, telegram_client, qq_client
        )
        app.state.services = AppServices(
            config_manager,
            repository,
            ingestion,
            polling,
            processor,
            scheduler,
            http,
            database,
        )
        app.state.control_services = control_services
        task = asyncio.create_task(scheduler.run())
        try:
            if mcp_app is None:
                yield
            else:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
        finally:
            await scheduler.stop()
            await task
            await http.aclose()
            await database.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(public_router)
    if mcp_token:
        app.state.console_token = mcp_token
        app.include_router(console_router)
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
    web_dist = _resolve_web_dist()
    if mcp_token and web_dist is not None:
        assets_dir = web_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
        web_root = web_dist.resolve()

        @app.get("/{full_path:path}")
        async def spa(full_path: str):
            reserved = ("api", "mcp", "health", "status")
            prefixes = tuple(f"{name}/" for name in reserved)
            if full_path in reserved or full_path.startswith(prefixes):
                raise HTTPException(status_code=404, detail="not found")
            if full_path:
                candidate = (web_dist / full_path).resolve()
                if candidate.is_file() and str(candidate).startswith(str(web_root)):
                    return FileResponse(candidate)
            index = web_dist / "index.html"
            if not index.is_file():
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(index)

    return app


def run() -> None:
    path = Path(os.environ.get("FEEDSENTRY_CONFIG", "config.yaml"))
    uvicorn.run(create_app(path), host="0.0.0.0", port=8000)
