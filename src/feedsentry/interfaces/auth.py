from __future__ import annotations

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
