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


class ServerMetrics(BaseModel):
    """A MongoDB server's latest serverStatus sample, with operation rates since the one
    before it (None when there's no earlier sample, or the server restarted in between)."""

    source_id: int
    captured_at: datetime
    uptime_seconds: float
    connections_current: int | None
    connections_available: int | None
    cache_bytes: int | None
    cache_max_bytes: int | None
    repl_state: str | None
    opcounters: dict[str, int]
    rates_per_second: dict[str, float] | None
    rate_seconds: float | None


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
    # MongoDB only (from the profiler): index keys and documents read to answer the
    # operations. None for PostgreSQL, which reports buffers instead.
    keys_examined: int | None = None
    docs_examined: int | None = None


class OperationSummary(OperationMetrics):
    """One row of the explorer: an operation (a fingerprint) and its metrics in the window."""

    id: int
    source_id: int
    source_name: str
    engine: SourceEngine
    # PostgreSQL: the normalised statement. MongoDB: the query shape as JSON (values are "?").
    query: str
    # MongoDB only: the collection ("shop.orders") and the command (find, aggregate, ...).
    namespace: str | None = None
    command: str | None = None
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


class MongoPlanStage(BaseModel):
    """One stage of a MongoDB winning plan (explain at queryPlanner verbosity, redacted).

    Values from the query are "?" (filters) or dropped; index bounds keep only full ranges.
    """

    path: str
    stage: str
    index_name: str | None = None
    key_pattern: str | None = None
    sort_pattern: str | None = None
    direction: str | None = None
    filter: str | None = None
    index_bounds: dict[str, list[str]] = {}
    # Every bounded field includes a full range: the scan reads the whole index.
    full_index_scan: bool = False
    is_multi_key: bool | None = None
    details: dict[str, str] = {}
    children: list[MongoPlanStage] = []


class OperationPlan(BaseModel):
    """The latest plan for an operation, or why there isn't one.

    PostgreSQL plans come from EXPLAIN (GENERIC_PLAN) and fill `root`; MongoDB plans come
    from explain at queryPlanner verbosity and fill `stage_root`.

    - captured: `root` and `raw` hold the plan.
    - failed: EXPLAIN was tried and `reason` says why it gave no plan.
    - not_captured: plans are available but none was captured yet for this operation.
    - unavailable: this source can't produce plans; `reason` and `fix` say why and what to do.
    """

    operation_id: int
    engine: SourceEngine = SourceEngine.POSTGRES
    status: PlanStatus
    plan_id: int | None = None
    captured_at: datetime | None = None
    checked_at: datetime | None = None
    reason: str | None = None
    fix: str | None = None
    root: PlanNode | None = None
    stage_root: MongoPlanStage | None = None
    # MongoDB: how many candidate plans lost to the winning one, and the aggregation stages
    # that run after the query engine.
    rejected_plans: int = 0
    pipeline_stages: list[str] = []
    raw: list[Any] | None = None
