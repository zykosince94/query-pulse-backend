"""Plan capture and storage, shared by the collector and the API.

The collector plans the busiest operations of each source in the background, so plans are
there when someone opens an operation (and when the rules look for evidence).

- PostgreSQL: EXPLAIN (GENERIC_PLAN) of the normalised statement. An admin can also
  re-capture one on demand.
- MongoDB: explain at queryPlanner verbosity of a recently profiled command, redacted
  (collector/mongo.py). Only the collector holds such commands, in memory, so there is no
  on-demand capture for MongoDB.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import psycopg
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.adapters.postgres import PlanUnavailableError, PostgresAdapter
from query_pulse.sources.models import DataSource, SourceCapability, SourceEngine
from query_pulse.workload import mongo_plan
from query_pulse.workload.models import Fingerprint, Observation, Plan
from query_pulse.workload.plan_tree import PlanFormatError, parse_plan
from query_pulse.workload.schemas import OperationPlan
from query_pulse.workload.service import as_utc

GENERIC_PLANS = "generic_plans"
MONGO_EXPLAIN = "explain"
Connection = psycopg.AsyncConnection[tuple[Any, ...]]


def plan_capability(source: DataSource) -> SourceCapability | None:
    feature = MONGO_EXPLAIN if source.engine is SourceEngine.MONGODB else GENERIC_PLANS
    return next((c for c in source.capabilities if c.feature == feature), None)


def plans_available(source: DataSource) -> bool:
    """Can plans be captured on demand (PostgreSQL generic plans)?"""
    capability = plan_capability(source)
    return (
        source.engine is SourceEngine.POSTGRES and capability is not None and capability.available
    )


def content_hash(plan: list[Any] | None, error: str | None) -> str:
    payload = json.dumps(plan, sort_keys=True) if plan is not None else f"error:{error}"
    return hashlib.sha256(payload.encode()).hexdigest()


async def latest_plan(session: AsyncSession, fingerprint_id: int) -> Plan | None:
    plan: Plan | None = await session.scalar(
        select(Plan)
        .where(Plan.fingerprint_id == fingerprint_id)
        .order_by(Plan.captured_at.desc(), Plan.id.desc())
        .limit(1)
    )
    return plan


async def explain(
    adapter: PostgresAdapter, conn: Connection, fingerprint: Fingerprint, username: str
) -> tuple[list[Any] | None, str | None]:
    """(plan, None) on success, (None, reason) when the statement has no plan."""
    try:
        return await adapter.explain_generic(conn, fingerprint.query, username), None
    except PlanUnavailableError as exc:
        return None, str(exc)


async def record_plan(
    session: AsyncSession,
    fingerprint: Fingerprint,
    plan: list[Any] | None,
    error: str | None,
    now: datetime,
) -> Plan:
    """Store a capture: a new row if the plan changed, otherwise mark the latest re-checked."""
    digest = content_hash(plan, error)
    latest = await latest_plan(session, fingerprint.id)
    if latest is not None and latest.content_hash == digest:
        latest.checked_at = now
        return latest
    row = Plan(
        fingerprint_id=fingerprint.id,
        source_id=fingerprint.source_id,
        engine=fingerprint.engine,
        plan=plan,
        error=error,
        content_hash=digest,
        captured_at=now,
        checked_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def top_fingerprints(
    session: AsyncSession, source_id: int, since: datetime, limit: int
) -> list[Fingerprint]:
    """The source's operations with the most total execution time since `since`."""
    total = func.sum(Observation.total_exec_time_ms)
    ranked = (
        select(Observation.fingerprint_id, total.label("total"))
        .where(Observation.source_id == source_id, Observation.interval_end > since)
        .group_by(Observation.fingerprint_id)
        .order_by(total.desc(), Observation.fingerprint_id)
        .limit(limit)
        .subquery()
    )
    rows = await session.scalars(
        select(Fingerprint)
        .join(ranked, ranked.c.fingerprint_id == Fingerprint.id)
        .order_by(ranked.c.total.desc(), Fingerprint.id)
    )
    return list(rows)


async def planned_ids(session: AsyncSession, fingerprint_ids: list[int]) -> set[int]:
    """Which of these fingerprints already have a plan row (captured or failed)."""
    if not fingerprint_ids:
        return set()
    rows = await session.scalars(
        select(Plan.fingerprint_id).where(Plan.fingerprint_id.in_(fingerprint_ids)).distinct()
    )
    return set(rows)


def to_out(fingerprint: Fingerprint, source: DataSource, plan: Plan | None) -> OperationPlan:
    """What the plan viewer shows: the plan, or exactly why there isn't one."""
    base: dict[str, Any] = {"operation_id": fingerprint.id, "engine": source.engine}
    mongo = source.engine is SourceEngine.MONGODB
    capability = plan_capability(source)
    if capability is not None and not capability.available:
        return OperationPlan(
            **base, status="unavailable", reason=capability.reason, fix=capability.fix
        )
    if plan is None:
        reason = (
            "No plan captured yet. The collector explains the busiest shapes of each source "
            "from an operation the profiler recorded. After a collector restart, a shape is "
            "explained again once the profiler records it again (Query Pulse never stores "
            "the query values needed to replay it)."
            if mongo
            else "No plan captured yet. The collector plans the busiest operations of each "
            "source every few minutes."
        )
        return OperationPlan(**base, status="not_captured", reason=reason)
    times = {"captured_at": as_utc(plan.captured_at), "checked_at": as_utc(plan.checked_at)}
    if plan.plan is None:
        return OperationPlan(**base, **times, status="failed", plan_id=plan.id, reason=plan.error)
    if mongo:
        try:
            stage_root = mongo_plan.parse_plan(plan.plan)
        except mongo_plan.MongoPlanFormatError as exc:
            return OperationPlan(
                **base, **times, status="failed", plan_id=plan.id, reason=f"Unreadable plan: {exc}"
            )
        return OperationPlan(
            **base,
            **times,
            status="captured",
            plan_id=plan.id,
            stage_root=stage_root,
            rejected_plans=mongo_plan.rejected_count(plan.plan),
            pipeline_stages=mongo_plan.pipeline_stages(plan.plan),
            raw=plan.plan,
        )
    try:
        root = parse_plan(plan.plan)
    except PlanFormatError as exc:
        return OperationPlan(
            **base, **times, status="failed", plan_id=plan.id, reason=f"Unreadable plan: {exc}"
        )
    return OperationPlan(
        **base, **times, status="captured", plan_id=plan.id, root=root, raw=plan.plan
    )
