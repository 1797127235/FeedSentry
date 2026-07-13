from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from feedsentry.ai import AIClient
from feedsentry.api import router
from feedsentry.apprise import AppriseClient
from feedsentry.config import ConfigManager
from feedsentry.database import Database, create_database
from feedsentry.feeds import FeedClient
from feedsentry.firecrawl import FirecrawlClient
from feedsentry.ingestion import IngestionService
from feedsentry.logging import configure_logging
from feedsentry.polling import PollCoordinator
from feedsentry.processor import EventProcessor
from feedsentry.repository import Repository
from feedsentry.scheduler import Scheduler
from feedsentry.telegram import TelegramNotifier


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


def create_app(config_path: Path) -> FastAPI:
    configure_logging()

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
        ingestion = IngestionService(repository, feed_client)
        polling = PollCoordinator(repository, ingestion)

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
        )
        scheduler = Scheduler(config_manager, repository, polling, processor)
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
        task = asyncio.create_task(scheduler.run())
        try:
            yield
        finally:
            await scheduler.stop()
            await task
            await http.aclose()
            await database.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app


def run() -> None:
    path = Path(os.environ.get("FEEDSENTRY_CONFIG", "config.yaml"))
    uvicorn.run(create_app(path), host="0.0.0.0", port=8000)
