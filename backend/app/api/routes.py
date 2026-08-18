from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.lanes import LANES
from app.agent.service import ResearchService
from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class ResearchRequest(BaseModel):
    app_name: str = Field(min_length=1, max_length=200, description="A single app name.")
    force_refresh: bool = Field(
        default=False, description="Skip the cached report and research from scratch."
    )
    wait: bool = Field(
        default=False, description="Block until the run completes instead of returning a run id."
    )


def _service(request: Request) -> ResearchService:
    service: ResearchService | None = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(503, "Research service is not ready.")
    return service


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "db": getattr(request.app.state, "db_ready", False),
        "checkpointer": getattr(request.app.state, "checkpointer_ready", False),
    }


@router.get("/config")
async def config() -> dict[str, Any]:
    s = get_settings()
    return {
        "orchestrator_model": s.orchestrator_model,
        "worker_model": s.worker_model,
        "orchestrator_effort": s.orchestrator_effort,
        "max_tool_calls_per_lane": s.max_tool_calls_per_lane,
        "max_source_chars": s.max_source_chars,
        "max_verify_retries": s.max_verify_retries,
        "report_cache_ttl_hours": s.report_cache_ttl_hours,
        "toolkits": s.toolkit_list,
        "lanes": [{"key": lane.key, "label": lane.label} for lane in LANES],
    }


@router.post("/research")
async def research(body: ResearchRequest, request: Request) -> dict[str, Any]:
    service = _service(request)
    if body.wait:
        try:
            return await service.execute(body.app_name, force_refresh=body.force_refresh)
        except Exception as exc:
            raise HTTPException(502, f"Research run failed: {exc}") from exc

    run_id = service.start_background(body.app_name, force_refresh=body.force_refresh)
    return {"run_id": run_id, "status": "running", "app_name": body.app_name}


@router.get("/research/{run_id}/events")
async def research_events(run_id: str, request: Request) -> EventSourceResponse:
    service = _service(request)

    async def generator():
        async for event in service.stream(run_id):
            if await request.is_disconnected():
                break
            yield {"event": event.get("type", "message"), "data": json.dumps(event)}

    return EventSourceResponse(generator())


@router.get("/runs")
async def list_runs(request: Request, limit: int = Query(default=50, ge=1, le=200)):
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(503, "Persistence is not configured.")
    return {"runs": await repo.list_runs(limit=limit)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(503, "Persistence is not configured.")
    run = await repo.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"No run {run_id}")
    return run


@router.get("/apps/{app_name}")
async def get_app(app_name: str, request: Request) -> dict[str, Any]:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(503, "Persistence is not configured.")
    report = await repo.latest_report(app_name)
    if report is None:
        raise HTTPException(404, f"No report stored for {app_name!r}")
    return report
