from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from query_pulse.accounts.dependencies import Writer, current_user
from query_pulse.sources.preview import demo_workload_preview
from query_pulse.sources.registry import SourceRegistry
from query_pulse.sources.schemas import (
    SourceConnectionRequest,
    SourceCreate,
    SourceHealth,
    SourceOut,
)

# Every route here requires a session; write routes additionally depend on `Writer`.
router = APIRouter(prefix="/api", tags=["sources"], dependencies=[Depends(current_user)])


def _registry(request: Request) -> SourceRegistry:
    registry: SourceRegistry = request.app.state.source_registry
    return registry


def _get_or_404(request: Request, source_id: str) -> SourceOut:
    source = _registry(request).get(source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found")
    return source


@router.get("/sources", operation_id="listSources")
async def list_sources(request: Request) -> dict[str, Any]:
    items = _registry(request).list()
    return {"count": len(items), "items": [item.model_dump(mode="json") for item in items]}


@router.post(
    "/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=SourceOut,
    operation_id="createSource",
)
async def create_source(payload: SourceCreate, request: Request, _: Writer) -> SourceOut:
    try:
        return _registry(request).add(payload)
    except KeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Source already exists") from None


@router.post("/sources/test", operation_id="testSourceConnection")
async def test_source_connection(payload: SourceConnectionRequest, _: Writer) -> dict[str, Any]:
    # Phase 2 replaces this with a real connect + version + capability check via the adapters.
    return {
        "status": "validated",
        "engine": payload.type.value,
        "connected": False,
        "message": "Parameters are valid. Live connection checks arrive in Phase 2.",
    }


@router.get(
    "/sources/{source_id}/health", response_model=SourceHealth, operation_id="getSourceHealth"
)
async def source_health(source_id: str, request: Request) -> SourceHealth:
    source = _get_or_404(request, source_id)
    return SourceHealth(
        source_id=source.id,
        status="unknown",
        engine=source.type,
        ready=False,
        message="Health is not checked yet; live checks arrive in Phase 2.",
    )


@router.get("/sources/{source_id}/workload-preview", operation_id="getWorkloadPreview")
async def workload_preview(source_id: str, request: Request) -> dict[str, Any]:
    return demo_workload_preview(_get_or_404(request, source_id))


@router.get("/collector/status", operation_id="getCollectorStatus")
async def collector_status(request: Request) -> dict[str, Any]:
    sources = _registry(request).list()
    return {
        "status": "not_running",
        "read_only": True,
        "source_count": len(sources),
        "engines": sorted({s.type.value for s in sources}),
        "message": "The collector process arrives in Phase 3.",
    }
