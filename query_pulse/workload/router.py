from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from query_pulse.accounts.dependencies import SessionDep, current_user
from query_pulse.collector.models import CollectorHeartbeat
from query_pulse.config import Settings
from query_pulse.sources import service as sources
from query_pulse.sources.models import DataSource
from query_pulse.sources.schemas import CollectorStatus
from query_pulse.workload import service
from query_pulse.workload.schemas import SourceHealth, SourceTrend

router = APIRouter(prefix="/api", tags=["workload"], dependencies=[Depends(current_user)])

Minutes = Annotated[int, Query(ge=5, le=1440, description="Window length in minutes")]


@router.get("/sources/{source_id}/trend", response_model=SourceTrend, operation_id="getSourceTrend")
async def source_trend(source_id: int, session: SessionDep, minutes: Minutes = 60) -> SourceTrend:
    """Calls and execution time per minute for one source."""
    if await session.get(DataSource, source_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found")
    points = (await service.trends(session, [source_id], minutes))[source_id]
    return SourceTrend(source_id=source_id, minutes=minutes, bucket_seconds=60, points=points)


@router.get("/workload/health", response_model=list[SourceHealth], operation_id="getWorkloadHealth")
async def workload_health(session: SessionDep, minutes: Minutes = 60) -> list[SourceHealth]:
    """The health strip: every source with its status and recent activity."""
    all_sources = list(
        await session.scalars(select(DataSource).order_by(func.lower(DataSource.name)))
    )
    trends = await service.trends(session, [s.id for s in all_sources], minutes)
    return [
        SourceHealth(
            source=sources.to_out(s),
            calls=sum(p.calls for p in trends[s.id]),
            total_exec_time_ms=sum(p.total_exec_time_ms for p in trends[s.id]),
            points=trends[s.id],
        )
        for s in all_sources
    ]


@router.get("/collector/status", response_model=CollectorStatus, operation_id="getCollectorStatus")
async def collector_status(request: Request, session: SessionDep) -> CollectorStatus:
    """Whether the collector process is alive, from the heartbeat it writes."""
    settings: Settings = request.app.state.settings
    beat = await session.get(CollectorHeartbeat, 1)
    count = await session.scalar(select(func.count()).select_from(DataSource)) or 0
    if beat is None:
        return CollectorStatus(
            status="not_running",
            last_beat_at=None,
            source_count=count,
            message="The collector has never run. Start it with `python -m query_pulse.collector`.",
        )
    last = service.as_utc(beat.beat_at)
    # The supervisor beats every collector_refresh_seconds; allow three missed beats.
    alive = datetime.now(UTC) - last <= timedelta(seconds=3 * settings.collector_refresh_seconds)
    return CollectorStatus(
        status="running" if alive else "not_running",
        last_beat_at=last,
        source_count=count,
        message="Collecting." if alive else "No heartbeat recently: the collector isn't running.",
    )
