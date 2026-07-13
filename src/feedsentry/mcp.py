from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


@dataclass
class ControlServices:
    sources: Any = None
    filter: Any = None
    status: Any = None
    recovery: Any = None
    destination: Any = None


class BearerTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, self.token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


class RequestLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        max_request_bytes: int,
        max_concurrent_requests: int,
    ) -> None:
        super().__init__(app)
        self.max_request_bytes = max_request_bytes
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > self.max_request_bytes
            except ValueError:
                return JSONResponse({"detail": "invalid content length"}, status_code=400)
            if too_large:
                return JSONResponse({"detail": "request too large"}, status_code=413)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > self.max_request_bytes:
                return JSONResponse({"detail": "request too large"}, status_code=413)
        request._body = bytes(content)
        async with self.semaphore:
            return await call_next(request)


def create_mcp_app(
    services: ControlServices,
    *,
    token: str,
    allowed_hosts: list[str] | None = None,
    max_request_bytes: int = 1_000_000,
    max_concurrent_requests: int = 10,
) -> Starlette:
    server = FastMCP(
        "FeedSentry",
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts or []),
    )

    @server.tool()
    async def discover_feeds(page_url: str) -> Any:
        """Discover RSSHub feeds available for a platform page URL."""
        return {
            "candidates": _serialize(
                await _require(services.sources, "sources").discover_feeds(page_url)
            )
        }

    @server.tool()
    async def subscribe_feed(candidate_id: str) -> Any:
        """Subscribe to a previously discovered RSSHub feed candidate."""
        return _serialize(await _require(services.sources, "sources").subscribe_feed(candidate_id))

    @server.tool()
    async def add_feed(url: str) -> Any:
        """Validate and subscribe to a direct RSS or Atom feed URL."""
        return _serialize(await _require(services.sources, "sources").add_feed(url))

    @server.tool()
    async def list_sources() -> Any:
        """List configured sources and their current health."""
        return {"sources": _serialize(await _require(services.sources, "sources").list_sources())}

    @server.tool()
    async def set_source_enabled(source_id: str, enabled: bool) -> Any:
        """Enable or disable a configured source."""
        return {
            "changed": await _require(services.sources, "sources").set_enabled(source_id, enabled)
        }

    @server.tool()
    async def remove_source(source_id: str) -> Any:
        """Remove a source while preserving its stored processing history."""
        return {"removed": await _require(services.sources, "sources").remove(source_id)}

    @server.tool()
    async def check_source_now(source_id: str) -> Any:
        """Immediately check a configured source for new entries."""
        return {"created_events": await _require(services.sources, "sources").check_now(source_id)}

    @server.tool()
    async def get_filter_goal() -> Any:
        """Return the global AI filtering goal."""
        return {"goal": _require(services.filter, "filter").get_goal()}

    @server.tool()
    async def set_filter_goal(goal: str) -> Any:
        """Replace the global AI filtering goal for future entries."""
        return {"changed": await _require(services.filter, "filter").set_goal(goal)}

    @server.tool()
    async def get_status() -> Any:
        """Return system and source health status."""
        return _serialize(await _require(services.status, "status").get_status())

    @server.tool()
    async def list_failed_events() -> Any:
        """List terminally failed processing events."""
        return {
            "events": _serialize(await _require(services.recovery, "recovery").list_failed_events())
        }

    @server.tool()
    async def retry_failed_event(event_id: int) -> Any:
        """Retry a terminal event from its recorded failed stage."""
        return {
            "retried": await _require(services.recovery, "recovery").retry_failed_event(event_id)
        }

    @server.tool()
    async def test_destination() -> Any:
        """Send an explicitly marked FeedSentry test notification."""
        return {"response": await _require(services.destination, "destination").test()}

    app = server.streamable_http_app()
    app.user_middleware.insert(0, Middleware(BearerTokenMiddleware, token=token))
    app.user_middleware.insert(
        1,
        Middleware(
            RequestLimitMiddleware,
            max_request_bytes=max_request_bytes,
            max_concurrent_requests=max_concurrent_requests,
        ),
    )
    app.middleware_stack = app.build_middleware_stack()
    return app


def _require(service: Any, name: str) -> Any:
    if service is None:
        raise RuntimeError(f"{name} control service is unavailable")
    return service


def _serialize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value
