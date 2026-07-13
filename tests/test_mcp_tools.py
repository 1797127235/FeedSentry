from __future__ import annotations

import json

from test_mcp_transport import mcp_client


async def test_get_status_calls_injected_service() -> None:
    async with mcp_client() as session:
        result = await session.call_tool("get_status")

    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload == {"status": "ok"}


async def test_list_sources_calls_injected_service() -> None:
    async with mcp_client() as session:
        result = await session.call_tool("list_sources")

    assert result.isError is False
    assert json.loads(result.content[0].text) == {"sources": []}
