from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from feedsentry.interfaces.api import console_router, public_router
from feedsentry.interfaces.mcp import ControlServices


@dataclass
class FakeStatus:
    async def get_status(self):
        return {
            "sources": 1,
            "enabled_sources": 1,
            "pending_events": 0,
            "failed_events": 0,
            "config_error": None,
            "source_statuses": [],
            "last_tick_at": None,
            "status_counts": {"filtered": 1},
        }

    async def list_events(self, **kwargs):
        return [], None

    async def get_event(self, event_id: int):
        raise LookupError(event_id)


@dataclass
class FakeSources:
    enabled: dict[str, bool]

    async def list_sources(self):
        return []

    async def set_enabled(self, source_id: str, enabled: bool) -> bool:
        self.enabled[source_id] = enabled
        return True

    async def remove(self, source_id: str) -> bool:
        return source_id in self.enabled

    async def check_now(self, source_id: str) -> int:
        return 2

    async def discover_feeds(self, page_url: str):
        return []

    async def subscribe_feed(self, candidate_id: str):
        raise ValueError("invalid candidate")

    async def add_feed(self, url: str):
        raise ValueError("bad feed")


@dataclass
class FakeFilter:
    goal: str = "track AI"

    def get_goal(self) -> str:
        return self.goal

    async def set_goal(self, goal: str) -> bool:
        self.goal = goal
        return True

    async def append_goal(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            raise ValueError("appended filter goal must not be empty")
        lines = [line.strip() for line in self.goal.splitlines() if line.strip()]
        if normalized in lines:
            return False
        self.goal = f"{self.goal}\n{normalized}" if self.goal else normalized
        return True


@dataclass
class FakeRecovery:
    retried: list[int]

    async def list_failed_events(self):
        return [
            {
                "event_id": 7,
                "entry_id": 3,
                "title": "failed item",
                "failed_stage": "ai",
                "failure_count": 3,
                "last_error": "timeout",
                "updated_at": datetime(2026, 7, 15, tzinfo=UTC),
            }
        ]

    async def retry_failed_event(self, event_id: int) -> bool:
        self.retried.append(event_id)
        return True


@dataclass
class FakeDestination:
    async def test(self) -> str:
        return "ok"


def build_app(token: str | None = "secret") -> FastAPI:
    app = FastAPI()
    app.include_router(public_router)
    if token:
        app.include_router(console_router)
        app.state.console_token = token
        app.state.control_services = ControlServices(
            sources=FakeSources(enabled={"src-1": True}),
            filter=FakeFilter(),
            status=FakeStatus(),
            recovery=FakeRecovery(retried=[]),
            destination=FakeDestination(),
        )
    return app


def auth(token: str = "secret") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_api_requires_bearer() -> None:
    app = build_app("secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/api/status")
        wrong = await client.get("/api/status", headers={"Authorization": "Bearer wrong"})
        ok = await client.get("/api/status", headers=auth())
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["status_counts"]["filtered"] == 1


async def test_public_live_does_not_require_bearer() -> None:
    app = build_app("secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/health/live")
    assert live.json() == {"status": "ok"}


async def test_events_failed_not_captured_by_event_id() -> None:
    app = build_app("secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/events/failed", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["events"][0]["event_id"] == 7


async def test_missing_event_returns_404() -> None:
    app = build_app("secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/events/99", headers=auth())
    assert response.status_code == 404


async def test_set_source_enabled_and_retry() -> None:
    app = build_app("secret")
    services = app.state.control_services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        patch = await client.patch(
            "/api/sources/src-1",
            headers=auth(),
            json={"enabled": False},
        )
        retry = await client.post("/api/events/7/retry", headers=auth())
    assert patch.status_code == 200
    assert patch.json() == {"changed": True}
    assert services.sources.enabled["src-1"] is False
    assert retry.status_code == 200
    assert retry.json() == {"retried": True}
    assert services.recovery.retried == [7]


async def test_list_events_and_filter() -> None:
    app = build_app("secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        events = await client.get("/api/events", headers=auth())
        filt = await client.get("/api/filter", headers=auth())
        put = await client.put("/api/filter", headers=auth(), json={"goal": "new goal"})
    assert events.status_code == 200
    assert events.json() == {"items": [], "next_cursor": None}
    assert filt.json() == {"goal": "track AI"}
    assert put.json() == {"changed": True}


async def test_append_filter_joins_and_idempotent() -> None:
    app = build_app("secret")
    services = app.state.control_services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/api/filter/append",
            headers=auth(),
            json={"text": "new direction"},
        )
        duplicate = await client.post(
            "/api/filter/append",
            headers=auth(),
            json={"text": "  new direction  "},
        )
        blank = await client.post(
            "/api/filter/append",
            headers=auth(),
            json={"text": "   "},
        )
    assert first.status_code == 200
    assert first.json() == {"changed": True}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"changed": False}
    assert blank.status_code == 400
    assert services.filter.goal == "track AI\nnew direction"


async def test_validation_error_returns_400() -> None:
    app = build_app("secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/feeds",
            headers=auth(),
            json={"url": "http://bad.example/feed"},
        )
    assert response.status_code == 400


async def test_missing_nested_control_service_returns_503() -> None:
    app = build_app("secret")
    app.state.control_services = ControlServices(sources=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/sources", headers=auth())
    assert response.status_code == 503
    assert "sources" in response.json()["detail"]


async def test_console_routes_absent_without_token(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FEEDSENTRY_MCP_TOKEN", raising=False)
    from feedsentry.app import create_app

    config_path = tmp_path / "config.yaml"
    config_path.write_text("placeholder: true\n", encoding="utf-8")
    app = create_app(config_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        api_status = await client.get("/api/status")
        live = await client.get("/health/live")
    assert api_status.status_code == 404
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}


def _write_runtime_config(tmp_path) -> Path:
    from conftest import VALID_CONFIG

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "data" / "feedsentry.db"
    content = VALID_CONFIG.replace(
        "path: ./data/test.db",
        f"path: {db_path.as_posix()}",
    ).replace(
        "sources:\n"
        "  - id: example\n"
        "    kind: feed\n"
        "    url: https://example.com/feed.xml\n"
        "    enabled: true\n",
        "sources: []\n",
    )
    config_path.write_text(content, encoding="utf-8")
    return config_path


async def test_create_app_with_token_auth_and_public_routes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FEEDSENTRY_MCP_TOKEN", "secret")
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    monkeypatch.delenv("FEEDSENTRY_WEB_DIST", raising=False)
    from feedsentry.app import create_app

    app = create_app(_write_runtime_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            unauth = await client.get("/api/status")
            authorized = await client.get("/api/status", headers=auth())
            # Mounted MCP lives under /mcp (streamable path "/"); trailing slash hits the sub-app.
            mcp = await client.post("/mcp/")
            live = await client.get("/health/live")
            public_status = await client.get("/status")
    assert unauth.status_code == 401
    assert authorized.status_code == 200
    assert mcp.status_code == 401
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert public_status.status_code == 200
    assert public_status.json()["sources"] == 0


async def test_spa_serves_real_files_under_dist(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa-index</html>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg id='icon'/>", encoding="utf-8")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("window.APP=1", encoding="utf-8")

    monkeypatch.setenv("FEEDSENTRY_MCP_TOKEN", "secret")
    monkeypatch.setenv("FEEDSENTRY_WEB_DIST", str(dist))
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    from feedsentry.app import create_app

    app = create_app(_write_runtime_config(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        favicon = await client.get("/favicon.svg")
        root = await client.get("/")
        deep = await client.get("/sources")
        asset = await client.get("/assets/app.js")
        api = await client.get("/api/status")
    assert favicon.status_code == 200
    assert favicon.text == "<svg id='icon'/>"
    assert "spa-index" in root.text
    assert "spa-index" in deep.text
    assert asset.status_code == 200
    assert asset.text == "window.APP=1"
    assert api.status_code == 401
