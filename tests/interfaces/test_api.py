from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from feedsentry.api import router


class FakeRepository:
    async def ping(self) -> bool:
        return True

    async def status_counts(self):
        return type("Counts", (), {"pending": 2, "failed": 1})()


@dataclass
class FakeServices:
    config_manager: object
    repository: FakeRepository
    scheduler: object


async def test_health_and_status_do_not_expose_secrets() -> None:
    app = FastAPI()
    app.include_router(router)
    config = type(
        "ConfigManager",
        (),
        {
            "current": type(
                "Config",
                (),
                {
                    "sources": [
                        type("Source", (), {"enabled": True})(),
                        type("Source", (), {"enabled": False})(),
                    ]
                },
            )(),
            "last_error": None,
        },
    )()
    app.state.services = FakeServices(
        config, FakeRepository(), type("Scheduler", (), {"last_tick_at": None})()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        status = await client.get("/status")

    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert status.json()["sources"] == 2
    assert status.json()["enabled_sources"] == 1
    assert "secret-ai-key" not in status.text
