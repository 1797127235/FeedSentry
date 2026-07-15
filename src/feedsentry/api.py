from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from feedsentry.auth import require_bearer
from feedsentry.feed_validation import FeedValidationError
from feedsentry.mcp import ControlServices
from feedsentry.serialize import serialize_public

public_router = APIRouter()
console_router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer)])

# Backward-compatible alias used by existing tests / app wiring until Task 4.
router = public_router


class DiscoverBody(BaseModel):
    page_url: str


class SubscribeBody(BaseModel):
    candidate_id: str


class AddFeedBody(BaseModel):
    url: str


class SetEnabledBody(BaseModel):
    enabled: bool


class SetFilterBody(BaseModel):
    goal: str = Field(min_length=1)


def require_console_services(request: Request) -> ControlServices:
    services = getattr(request.app.state, "control_services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="control services unavailable")
    return services


def _require_service(service: Any, name: str) -> Any:
    """Fail closed when a nested control service was never wired (mirrors MCP `_require`)."""
    if service is None:
        raise HTTPException(status_code=503, detail=f"{name} control service is unavailable")
    return service


def _map_control_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc) or "not found")
    if isinstance(exc, FeedValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=503, detail=str(exc))
    raise exc


async def _call(coro):
    try:
        return await coro
    except (LookupError, FeedValidationError, ValueError, RuntimeError) as exc:
        raise _map_control_error(exc) from exc


@public_router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@public_router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    services = request.app.state.services
    if services.config_manager.current is None or not await services.repository.ping():
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


@public_router.get("/status")
async def status(request: Request) -> dict[str, object]:
    services = request.app.state.services
    counts = await services.repository.status_counts()
    sources = services.config_manager.current.sources
    return {
        "sources": len(sources),
        "enabled_sources": sum(source.enabled for source in sources),
        "last_tick_at": services.scheduler.last_tick_at,
        "pending_events": counts.pending,
        "failed_events": counts.failed,
        "config_error": services.config_manager.last_error,
    }


@console_router.get("/status")
async def api_status(request: Request) -> Any:
    services = require_console_services(request)
    result = await _call(_require_service(services.status, "status").get_status())
    return serialize_public(result)


@console_router.get("/sources")
async def api_list_sources(request: Request) -> Any:
    services = require_console_services(request)
    result = await _call(_require_service(services.sources, "sources").list_sources())
    return {"sources": serialize_public(result)}


@console_router.get("/filter")
async def api_get_filter(request: Request) -> Any:
    services = require_console_services(request)
    try:
        goal = _require_service(services.filter, "filter").get_goal()
    except (LookupError, FeedValidationError, ValueError, RuntimeError) as exc:
        raise _map_control_error(exc) from exc
    return {"goal": goal}


@console_router.get("/events")
async def api_list_events(
    request: Request,
    status: str | None = None,
    source_id: str | None = None,
    q: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Any:
    services = require_console_services(request)
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    status_svc = _require_service(services.status, "status")
    items, next_cursor = await _call(
        status_svc.list_events(
            status=status,
            source_id=source_id,
            q=q,
            limit=limit,
            cursor=cursor,
        )
    )
    return {"items": serialize_public(items), "next_cursor": next_cursor}


@console_router.get("/events/failed")
async def api_list_failed_events(request: Request) -> Any:
    services = require_console_services(request)
    result = await _call(_require_service(services.recovery, "recovery").list_failed_events())
    return {"events": serialize_public(result)}


@console_router.get("/events/{event_id}")
async def api_get_event(request: Request, event_id: int) -> Any:
    services = require_console_services(request)
    result = await _call(_require_service(services.status, "status").get_event(event_id))
    return serialize_public(result)


@console_router.post("/feeds/discover")
async def api_discover_feeds(request: Request, body: DiscoverBody) -> Any:
    services = require_console_services(request)
    sources = _require_service(services.sources, "sources")
    result = await _call(sources.discover_feeds(body.page_url))
    return {"candidates": serialize_public(result)}


@console_router.post("/feeds/subscribe")
async def api_subscribe_feed(request: Request, body: SubscribeBody) -> Any:
    services = require_console_services(request)
    result = await _call(
        _require_service(services.sources, "sources").subscribe_feed(body.candidate_id)
    )
    return serialize_public(result)


@console_router.post("/feeds")
async def api_add_feed(request: Request, body: AddFeedBody) -> Any:
    services = require_console_services(request)
    result = await _call(_require_service(services.sources, "sources").add_feed(body.url))
    return serialize_public(result)


@console_router.patch("/sources/{source_id}")
async def api_set_source_enabled(request: Request, source_id: str, body: SetEnabledBody) -> Any:
    services = require_console_services(request)
    changed = await _call(
        _require_service(services.sources, "sources").set_enabled(source_id, body.enabled)
    )
    return {"changed": changed}


@console_router.delete("/sources/{source_id}")
async def api_remove_source(request: Request, source_id: str) -> Any:
    services = require_console_services(request)
    removed = await _call(_require_service(services.sources, "sources").remove(source_id))
    return {"removed": removed}


@console_router.post("/sources/{source_id}/check")
async def api_check_source(request: Request, source_id: str) -> Any:
    services = require_console_services(request)
    created = await _call(_require_service(services.sources, "sources").check_now(source_id))
    return {"created_events": created}


@console_router.put("/filter")
async def api_set_filter(request: Request, body: SetFilterBody) -> Any:
    services = require_console_services(request)
    changed = await _call(_require_service(services.filter, "filter").set_goal(body.goal))
    return {"changed": changed}


@console_router.post("/events/{event_id}/retry")
async def api_retry_event(request: Request, event_id: int) -> Any:
    services = require_console_services(request)
    retried = await _call(
        _require_service(services.recovery, "recovery").retry_failed_event(event_id)
    )
    return {"retried": retried}


@console_router.post("/destination/test")
async def api_test_destination(request: Request) -> Any:
    services = require_console_services(request)
    response = await _call(_require_service(services.destination, "destination").test())
    return {"response": response}
