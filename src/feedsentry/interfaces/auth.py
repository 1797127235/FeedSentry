from __future__ import annotations

import asyncio
import secrets

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response


def verify_bearer(authorization: str | None, token: str) -> bool:
    if not authorization:
        return False
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    # compare_digest requires equal-length strings
    if len(supplied) != len(token):
        return False
    return secrets.compare_digest(supplied, token)


async def require_bearer(request: Request) -> None:
    token = getattr(request.app.state, "console_token", None)
    if not token:
        raise HTTPException(status_code=404, detail="not found")
    authorization = request.headers.get("authorization")
    if not verify_bearer(authorization, token):
        raise HTTPException(status_code=401, detail="unauthorized")


class BearerTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        authorization = request.headers.get("authorization")
        if not verify_bearer(authorization, self.token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)


class ConsoleRequestLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        max_request_bytes: int = 1_000_000,
        max_concurrent_requests: int = 10,
    ) -> None:
        super().__init__(app)
        self.max_request_bytes = max_request_bytes
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_request_bytes:
                    return JSONResponse({"detail": "request too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "invalid content length"}, status_code=400)
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > self.max_request_bytes:
                return JSONResponse({"detail": "request too large"}, status_code=413)
            content.extend(chunk)
        request._body = bytes(content)
        async with self.semaphore:
            return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
