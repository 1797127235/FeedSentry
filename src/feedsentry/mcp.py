from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette


def create_mcp_app(*, allowed_hosts: list[str] | None = None) -> Starlette:
    server = FastMCP(
        "FeedSentry",
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts or []),
    )

    @server.tool()
    async def get_status() -> dict[str, str]:
        """Return the FeedSentry service status."""
        return {"status": "ok"}

    return server.streamable_http_app()
