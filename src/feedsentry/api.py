from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    services = request.app.state.services
    if services.config_manager.current is None or not await services.repository.ping():
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ready"}


@router.get("/status")
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
