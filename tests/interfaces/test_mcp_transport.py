from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from feedsentry.mcp import ControlServices, create_mcp_app


class FakeSources:
    async def list_sources(self):
        return []


class FakeStatus:
    async def get_status(self):
        return {"status": "ok"}


@asynccontextmanager
async def mcp_client(token: str = "secret") -> AsyncIterator[ClientSession]:
    services = ControlServices(sources=FakeSources(), status=FakeStatus())
    app = create_mcp_app(services, token=token, allowed_hosts=["localhost"])
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": f"Bearer {token}"},
    )
    async with app.router.lifespan_context(app):
        async with http:
            async with streamable_http_client("http://localhost/", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session


async def test_streamable_http_initializes_and_lists_tools() -> None:
    async with mcp_client() as session:
        tools = await session.list_tools()

    assert {tool.name for tool in tools.tools} == {
        "discover_feeds",
        "subscribe_feed",
        "add_feed",
        "list_sources",
        "set_source_enabled",
        "remove_source",
        "check_source_now",
        "get_filter_goal",
        "set_filter_goal",
        "get_status",
        "list_failed_events",
        "retry_failed_event",
        "test_destination",
    }


async def test_mcp_rejects_missing_and_wrong_bearer_tokens() -> None:
    app = create_mcp_app(
        ControlServices(sources=FakeSources(), status=FakeStatus()),
        token="secret",
        allowed_hosts=["localhost"],
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as http:
            missing = await http.post("/", json={})
            wrong = await http.post("/", json={}, headers={"Authorization": "Bearer wrong"})
    assert missing.status_code == 401
    assert wrong.status_code == 401


async def test_mcp_rejects_oversized_request() -> None:
    app = create_mcp_app(
        ControlServices(sources=FakeSources(), status=FakeStatus()),
        token="secret",
        allowed_hosts=["localhost"],
        max_request_bytes=10,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": "Bearer secret"},
        ) as http:
            response = await http.post("/", content=b"x" * 11)
    assert response.status_code == 413


async def test_mcp_rejects_oversized_streamed_request_without_content_length() -> None:
    app = create_mcp_app(
        ControlServices(sources=FakeSources(), status=FakeStatus()),
        token="secret",
        allowed_hosts=["localhost"],
        max_request_bytes=10,
    )

    async def chunks():
        yield b"x" * 6
        yield b"y" * 6

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": "Bearer secret"},
        ) as http:
            response = await http.post("/", content=chunks())
    assert response.status_code == 413
