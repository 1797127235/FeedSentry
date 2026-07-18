from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from feedsentry.interfaces.auth import BearerTokenMiddleware
from feedsentry.interfaces.serialize import serialize_public

# Re-export for callers that imported BearerTokenMiddleware from mcp.
__all__ = [
    "BearerTokenMiddleware",
    "ControlServices",
    "RequestLimitMiddleware",
    "create_mcp_app",
]


@dataclass
class ControlServices:
    sources: Any = None
    filter: Any = None
    status: Any = None
    recovery: Any = None
    destination: Any = None


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
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts or []),
    )

    @server.tool()
    async def discover_feeds(page_url: str) -> Any:
        """根据平台页面 URL，通过 RSSHub Radar 发现可订阅的信息源。"""
        return {
            "candidates": serialize_public(
                await _require(services.sources, "sources").discover_feeds(page_url)
            )
        }

    @server.tool()
    async def subscribe_feed(candidate_id: str) -> Any:
        """订阅先前发现的 RSSHub 候选信息源，并静默建立基线。"""
        return serialize_public(
            await _require(services.sources, "sources").subscribe_feed(candidate_id)
        )

    @server.tool()
    async def add_feed(url: str) -> Any:
        """验证并订阅一个直接的 RSS 或 Atom 地址，并静默建立基线。"""
        return serialize_public(await _require(services.sources, "sources").add_feed(url))

    @server.tool()
    async def list_sources() -> Any:
        """列出所有已配置的信息源及其当前健康状态。"""
        return {
            "sources": serialize_public(await _require(services.sources, "sources").list_sources())
        }

    @server.tool()
    async def set_source_enabled(source_id: str, enabled: bool) -> Any:
        """启用或停用指定的信息源。"""
        return {
            "changed": await _require(services.sources, "sources").set_enabled(source_id, enabled)
        }

    @server.tool()
    async def remove_source(source_id: str) -> Any:
        """删除指定信息源，但保留已经存储的处理历史。"""
        return {"removed": await _require(services.sources, "sources").remove(source_id)}

    @server.tool()
    async def check_source_now(source_id: str) -> Any:
        """立即检查指定信息源是否有新条目。"""
        return {"created_events": await _require(services.sources, "sources").check_now(source_id)}

    @server.tool()
    async def get_filter_goal() -> Any:
        """获取当前全局 AI 筛选关注点。"""
        return {"goal": _require(services.filter, "filter").get_goal()}

    @server.tool()
    async def set_filter_goal(goal: str) -> Any:
        """修改全局 AI 筛选关注点，仅影响之后发现的新条目。"""
        return {"changed": await _require(services.filter, "filter").set_goal(goal)}

    @server.tool()
    async def append_filter_goal(text: str) -> Any:
        """向全局 AI 筛选关注点追加一段内容（换行分隔），仅影响之后发现的新条目。

        重复内容幂等无变化。
        """
        return {"changed": await _require(services.filter, "filter").append_goal(text)}

    @server.tool()
    async def get_status() -> Any:
        """获取系统、信息源和事件的当前状态。"""
        return serialize_public(await _require(services.status, "status").get_status())

    @server.tool()
    async def list_failed_events() -> Any:
        """列出经过重试后仍然失败的处理事件。"""
        return {
            "events": serialize_public(
                await _require(services.recovery, "recovery").list_failed_events()
            )
        }

    @server.tool()
    async def retry_failed_event(event_id: int) -> Any:
        """从已记录的失败阶段重新处理指定事件。"""
        return {
            "retried": await _require(services.recovery, "recovery").retry_failed_event(event_id)
        }

    @server.tool()
    async def test_destination() -> Any:
        """向当前通知目标发送一条明确标记为测试的 FeedSentry 通知。"""
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
