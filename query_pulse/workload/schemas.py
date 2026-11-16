from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from query_pulse.sources.models import SourceEngine
from query_pulse.sources.schemas import SourceOut


class TrendPoint(BaseModel):
    """Activity in one minute: total calls and total execution time across all statements."""

    start: datetime
    calls: int
    total_exec_time_ms: float


class SourceTrend(BaseModel):
    source_id: int
    minutes: int
    bucket_seconds: int
    points: list[TrendPoint]


class SourceHealth(BaseModel):
    """One card of the health strip: the source, its totals and its trend."""

    source: SourceOut
    calls: int
    total_exec_time_ms: float
    points: list[TrendPoint]


# --- workload explorer (Phase 4) -------------------------------------------------------------

SortKey = Literal["total_time", "mean_time", "calls"]
PlanStatus = Literal["captured", "failed", "not_captured", "unavailable"]


class OperationMetrics(BaseModel):
    """What one operation did across a time window (sums of its interval deltas).

    The mean is total time ÷ calls over the whole window, never an average of per-interval
    means (which would weigh a quiet interval the same as a busy one).
    """

    calls: int
    total_exec_time_ms: float
    mean_exec_time_ms: float
    rows: int
    shared_blks_hit: int
    shared_blks_read: int


class OperationSummary(OperationMetrics):
    """One row of the explorer: an operation (a fingerprint) and its metrics in the window."""

    id: int
    source_id: int
    source_name: str
    engine: SourceEngine
    query: str
    # This operation's share of the window's total execution time (0..1), on its source(s).
    share_of_total_time: float
    first_seen_at: datetime
    last_seen_at: datetime


class OperationList(BaseModel):
    start: datetime
    end: datetime
    sort: SortKey
    # How many operations matched (the list holds at most `limit` of them).
    total_count: int
    # Total execution time of every operation in the window (ignores the text filter).
    total_exec_time_ms: float
    operations: list[OperationSummary]


class HistoryPoint(BaseModel):
    """One bucket of an operation's history. The mean is None when there were no calls."""

    start: datetime
    calls: int
    total_exec_time_ms: float
    mean_exec_time_ms: float | None
    rows: int


class OperationDetail(BaseModel):
    operation: OperationSummary
    source: SourceOut
    toplevel: bool
    start: datetime
    end: datetime
    bucket_seconds: int
    history: list[HistoryPoint]


class PlanNode(BaseModel):
    """One node of a PostgreSQL plan, with the planner's estimates (nothing was executed)."""

    path: str
    node_type: str
    relation_name: str | None = None
    schema_name: str | None = None
    alias: str | None = None
    index_name: str | None = None
    startup_cost: float
    total_cost: float
    plan_rows: float
    plan_width: int
    filter: str | None = None
    index_cond: str | None = None
    recheck_cond: str | None = None
    join_cond: str | None = None
    sort_key: list[str] = []
    join_type: str | None = None
    strategy: str | None = None
    parent_relationship: str | None = None
    details: dict[str, str] = {}
    children: list[PlanNode] = []


class OperationPlan(BaseModel):
    """The latest generic plan for an operation, or why there isn't one.

    - captured: `root` and `raw` hold the plan.
    - failed: EXPLAIN was tried and `reason` says why it gave no plan.
    - not_captured: plans are available but none was captured yet for this operation.
    - unavailable: this source can't produce plans; `reason` and `fix` say why and what to do.
    """

    operation_id: int
    status: PlanStatus
    plan_id: int | None = None
    captured_at: datetime | None = None
    checked_at: datetime | None = None
    reason: str | None = None
    fix: str | None = None
    root: PlanNode | None = None
    raw: list[Any] | None = None
