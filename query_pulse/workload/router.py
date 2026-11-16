from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from query_pulse.accounts.dependencies import SessionDep, Writer, current_user
from query_pulse.adapters import AdapterError
from query_pulse.adapters.postgres import PostgresAdapter
from query_pulse.api_types import Id, IdQuery, invalid
from query_pulse.collector.models import CollectorHeartbeat
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import utcnow
from query_pulse.sources import service as sources
from query_pulse.sources.models import DataSource
from query_pulse.sources.schemas import CollectorStatus
from query_pulse.workload import explorer, plans, service
from query_pulse.workload.explorer import MAX_WINDOW, Window
from query_pulse.workload.models import Fingerprint, Plan, ServerSample
from query_pulse.workload.schemas import (
    OperationDetail,
    OperationList,
    OperationPlan,
    ServerMetrics,
    SortKey,
    SourceHealth,
    SourceTrend,
)

router = APIRouter(prefix="/api", tags=["workload"], dependencies=[Depends(current_user)])

Minutes = Annotated[int, Query(ge=5, le=1440, description="Window length in minutes")]


@router.get("/sources/{source_id}/trend", response_model=SourceTrend, operation_id="getSourceTrend")
async def source_trend(source_id: Id, session: SessionDep, minutes: Minutes = 60) -> SourceTrend:
    """Calls and execution time per minute for one source."""
    if await session.get(DataSource, source_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found")
    points = (await service.trends(session, [source_id], minutes))[source_id]
    return SourceTrend(source_id=source_id, minutes=minutes, bucket_seconds=60, points=points)


@router.get(
    "/sources/{source_id}/server",
    response_model=ServerMetrics | None,
    operation_id="getSourceServerMetrics",
)
async def source_server_metrics(source_id: Id, session: SessionDep) -> ServerMetrics | None:
    """A MongoDB source's latest serverStatus sample and operation rates (null if none yet)."""
    if await session.get(DataSource, source_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found")
    latest = list(
        await session.scalars(
            select(ServerSample)
            .where(ServerSample.source_id == source_id)
            .order_by(ServerSample.captured_at.desc(), ServerSample.id.desc())
            .limit(2)
        )
    )
    if not latest:
        return None
    current = latest[0]
    rates: dict[str, float] | None = None
    seconds: float | None = None
    if len(latest) == 2:
        before = latest[1]
        seconds = (
            service.as_utc(current.captured_at) - service.as_utc(before.captured_at)
        ).total_seconds()
        # A lower uptime means the server restarted and its counters started again.
        if seconds > 0 and current.uptime_seconds >= before.uptime_seconds:
            rates = {
                name: max(0, value - before.opcounters.get(name, 0)) / seconds
                for name, value in current.opcounters.items()
            }
        else:
            seconds = None
    return ServerMetrics(
        source_id=source_id,
        captured_at=service.as_utc(current.captured_at),
        uptime_seconds=current.uptime_seconds,
        connections_current=current.connections_current,
        connections_available=current.connections_available,
        cache_bytes=current.cache_bytes,
        cache_max_bytes=current.cache_max_bytes,
        repl_state=current.repl_state,
        opcounters=current.opcounters,
        rates_per_second=rates,
        rate_seconds=seconds,
    )


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


# --- workload explorer (Phase 4) -------------------------------------------------------------

MAX_MINUTES = int(MAX_WINDOW.total_seconds() // 60)


def time_window(
    minutes: Annotated[
        int | None,
        Query(ge=5, le=MAX_MINUTES, description="The last N minutes (default 60)"),
    ] = None,
    start: Annotated[
        datetime | None, Query(description="Absolute window start (ISO 8601; UTC if no zone)")
    ] = None,
    end: Annotated[datetime | None, Query(description="Absolute window end (default: now)")] = None,
) -> Window:
    """Either a relative window (`minutes`) or an absolute one (`start`, optional `end`)."""
    if minutes is not None and (start is not None or end is not None):
        raise invalid(("query", "minutes"), "Give either minutes or start/end, not both")
    if start is None:
        if end is not None:
            raise invalid(("query", "end"), "end needs a start")
        return Window.last(minutes or 60)
    window = Window(service.as_utc(start), service.as_utc(end) if end else datetime.now(UTC))
    if window.end - window.start < timedelta(minutes=1):
        raise invalid(("query", "start"), "The window must be at least one minute long")
    if window.end - window.start > MAX_WINDOW:
        raise invalid(("query", "start"), f"The window can't be longer than {MAX_WINDOW.days} days")
    return window


WindowDep = Annotated[Window, Depends(time_window)]


async def _operation_or_404(
    session: SessionDep, operation_id: int
) -> tuple[Fingerprint, DataSource]:
    fingerprint = await session.get(Fingerprint, operation_id)
    source = await session.get(DataSource, fingerprint.source_id) if fingerprint else None
    if fingerprint is None or source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Operation not found")
    return fingerprint, source


@router.get("/operations", response_model=OperationList, operation_id="listOperations")
async def list_operations(
    session: SessionDep,
    window: WindowDep,
    sort: SortKey = "total_time",
    source_id: IdQuery = None,
    q: Annotated[
        str | None, Query(max_length=200, description="Keep statements containing this text")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OperationList:
    """Operations ranked by total time, mean time or calls over a time window."""
    return await explorer.rank_operations(
        session, window, sort=sort, source_id=source_id, text=(q or "").strip() or None, limit=limit
    )


@router.get(
    "/operations/{operation_id}", response_model=OperationDetail, operation_id="getOperation"
)
async def get_operation(
    operation_id: Id, session: SessionDep, window: WindowDep
) -> OperationDetail:
    """One operation: its statement, metrics over the window and history per minute or hour."""
    fingerprint, source = await _operation_or_404(session, operation_id)
    return OperationDetail(
        operation=await explorer.operation_summary(session, fingerprint, source, window),
        source=sources.to_out(source),
        toplevel=fingerprint.toplevel is not False,
        start=window.start,
        end=window.end,
        bucket_seconds=int(window.step.total_seconds()),
        history=await explorer.operation_history(session, fingerprint.id, window),
    )


@router.get(
    "/operations/{operation_id}/plan",
    response_model=OperationPlan,
    operation_id="getOperationPlan",
)
async def get_operation_plan(operation_id: Id, session: SessionDep) -> OperationPlan:
    """The latest generic plan for an operation, or why there isn't one."""
    fingerprint, source = await _operation_or_404(session, operation_id)
    return plans.to_out(fingerprint, source, await plans.latest_plan(session, fingerprint.id))


@router.post(
    "/operations/{operation_id}/plan",
    response_model=OperationPlan,
    operation_id="captureOperationPlan",
    responses={502: {"description": "The source couldn't be reached to plan the statement"}},
)
async def capture_operation_plan(
    operation_id: Id, request: Request, session: SessionDep, _: Writer
) -> OperationPlan:
    """Plan the statement again now with EXPLAIN (GENERIC_PLAN). Never executes it.

    PostgreSQL only: MongoDB plans need a profiled command, which only the collector holds
    (in memory), so for a MongoDB operation this returns the stored plan unchanged.
    """
    fingerprint, source = await _operation_or_404(session, operation_id)
    if not plans.plans_available(source):
        return plans.to_out(fingerprint, source, await plans.latest_plan(session, fingerprint.id))
    settings: Settings = request.app.state.settings
    cipher: CredentialCipher = request.app.state.cipher
    adapter: PostgresAdapter = request.app.state.postgres_adapter
    try:
        params = sources.params_for(
            sources.connection_fields(source),
            sources.stored_password(source, cipher),
            settings.source_connect_timeout_seconds,
        )
        conn = await adapter.connect(params)
    except AdapterError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=error.message) from None
    async with conn:
        plan, reason = await plans.explain(adapter, conn, fingerprint, source.username)
    row = await plans.record_plan(session, fingerprint, plan, reason, utcnow())
    await session.commit()
    return plans.to_out(fingerprint, source, row)


@router.get("/plans/{plan_id}", response_model=OperationPlan, operation_id="getPlan")
async def get_plan(plan_id: Id, session: SessionDep) -> OperationPlan:
    """One stored plan by id, exactly as captured.

    Plans are immutable evidence: a finding cites the plan it was based on, and this returns
    that plan even after a newer one has been captured for the same operation.
    """
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plan not found")
    fingerprint, source = await _operation_or_404(session, plan.fingerprint_id)
    return plans.to_out(fingerprint, source, plan)
