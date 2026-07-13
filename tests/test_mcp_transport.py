from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from feedsentry.mcp import create_mcp_app


@asynccontextmanager
async def mcp_client() -> AsyncIterator[ClientSession]:
    app = create_mcp_app(allowed_hosts=["localhost"])

    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    )

    async with app.router.lifespan_context(app):
        async with http:
            async with streamable_http_client(
                "http://localhost/mcp",
                http_client=http,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session


async def test_streamable_http_initializes_and_lists_tools() -> None:
    async with mcp_client() as session:
        tools = await session.list_tools()

    assert [tool.name for tool in tools.tools] == ["get_status"]
