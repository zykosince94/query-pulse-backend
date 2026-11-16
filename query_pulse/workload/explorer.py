"""The workload explorer's read side: rank operations over a time window, and one
operation's metric history.

Everything is aggregated in SQL from the per-interval deltas the collector stored. An
observation belongs to a window when its interval *ended* inside it (start, end].
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import DateTime, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import FunctionElement

from query_pulse.sources.models import DataSource
from query_pulse.workload.models import Fingerprint, Observation
from query_pulse.workload.schemas import (
    HistoryPoint,
    OperationList,
    OperationMetrics,
    OperationSummary,
    SortKey,
)
from query_pulse.workload.service import as_utc

# Windows up to this long are bucketed per minute; longer ones per hour. Either way a chart
# gets at most a few hundred points (6 h = 360 minutes; 14 days = 336 hours).
MINUTE_BUCKETS_UP_TO = timedelta(hours=6)
MAX_WINDOW = timedelta(days=31)

Resolution = Literal["minute", "hour"]


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    @classmethod
    def last(cls, minutes: int, now: datetime | None = None) -> Window:
        end = now or datetime.now(UTC)
        return cls(end - timedelta(minutes=minutes), end)

    @property
    def resolution(self) -> Resolution:
        return "minute" if self.end - self.start <= MINUTE_BUCKETS_UP_TO else "hour"

    @property
    def step(self) -> timedelta:
        return timedelta(minutes=1) if self.resolution == "minute" else timedelta(hours=1)


# --- time buckets: date_trunc on PostgreSQL, strftime on SQLite (unit tests) ----------------


class _Trunc(FunctionElement[datetime]):
    type = DateTime(timezone=True)
    inherit_cache = True


class trunc_minute(_Trunc):  # noqa: N801 - reads like the SQL function it stands for
    name = "trunc_minute"
    inherit_cache = True


class trunc_hour(_Trunc):  # noqa: N801
    name = "trunc_hour"
    inherit_cache = True


# The three-argument date_trunc truncates in UTC whatever the session's TimeZone is (in a
# zone like Asia/Kolkata, +05:30, truncating to the hour in local time would put every bucket
# at half past in UTC).
@compiles(trunc_minute, "postgresql")
def _pg_minute(element: trunc_minute, compiler: SQLCompiler, **kw: Any) -> str:
    return f"date_trunc('minute', {compiler.process(element.clauses, **kw)}, 'UTC')"


@compiles(trunc_hour, "postgresql")
def _pg_hour(element: trunc_hour, compiler: SQLCompiler, **kw: Any) -> str:
    return f"date_trunc('hour', {compiler.process(element.clauses, **kw)}, 'UTC')"


@compiles(trunc_minute, "sqlite")
def _sqlite_minute(element: trunc_minute, compiler: SQLCompiler, **kw: Any) -> str:
    return f"strftime('%Y-%m-%d %H:%M:00', {compiler.process(element.clauses, **kw)})"


@compiles(trunc_hour, "sqlite")
def _sqlite_hour(element: trunc_hour, compiler: SQLCompiler, **kw: Any) -> str:
    return f"strftime('%Y-%m-%d %H:00:00', {compiler.process(element.clauses, **kw)})"


def floor_to(value: datetime, resolution: Resolution) -> datetime:
    value = as_utc(value).replace(second=0, microsecond=0)
    return value.replace(minute=0) if resolution == "hour" else value


# --- ranking ---------------------------------------------------------------------------------

_CALLS = func.sum(Observation.calls)
_TOTAL = func.sum(Observation.total_exec_time_ms)
_MEAN = func.coalesce(_TOTAL / func.nullif(_CALLS, 0), 0.0)
_ORDER: dict[str, ColumnElement[Any]] = {"total_time": _TOTAL, "mean_time": _MEAN, "calls": _CALLS}


def _in_window[S: Select[*tuple[Any, ...]]](query: S, window: Window) -> S:
    return query.where(
        Observation.interval_end > window.start, Observation.interval_end <= window.end
    )


def _metrics(row: Any) -> OperationMetrics:
    # PostgreSQL's sum(bigint) is numeric (a Decimal in Python); normalise the types.
    calls = int(row.calls or 0)
    total = float(row.total or 0.0)
    return OperationMetrics(
        calls=calls,
        total_exec_time_ms=total,
        mean_exec_time_ms=total / calls if calls else 0.0,
        rows=int(row.rows or 0),
        shared_blks_hit=int(row.hit or 0),
        shared_blks_read=int(row.read or 0),
    )


def _aggregate(window: Window) -> Select[*tuple[Any, ...]]:
    return _in_window(
        select(
            Observation.fingerprint_id.label("fingerprint_id"),
            _CALLS.label("calls"),
            _TOTAL.label("total"),
            func.sum(Observation.rows).label("rows"),
            func.sum(Observation.shared_blks_hit).label("hit"),
            func.sum(Observation.shared_blks_read).label("read"),
        ),
        window,
    ).group_by(Observation.fingerprint_id)


async def _window_total(
    session: AsyncSession, window: Window, source_ids: list[int] | None
) -> dict[int, float]:
    """Total execution time per source in the window."""
    query = _in_window(select(Observation.source_id, _TOTAL), window).group_by(
        Observation.source_id
    )
    if source_ids is not None:
        query = query.where(Observation.source_id.in_(source_ids))
    return {sid: float(total or 0.0) for sid, total in await session.execute(query)}


def _summary(
    fingerprint: Fingerprint, source: DataSource, metrics: OperationMetrics, source_total: float
) -> OperationSummary:
    return OperationSummary(
        **metrics.model_dump(),
        id=fingerprint.id,
        source_id=source.id,
        source_name=source.name,
        engine=source.engine,
        query=fingerprint.query,
        share_of_total_time=(metrics.total_exec_time_ms / source_total) if source_total else 0.0,
        first_seen_at=as_utc(fingerprint.first_seen_at),
        last_seen_at=as_utc(fingerprint.last_seen_at),
    )


async def rank_operations(
    session: AsyncSession,
    window: Window,
    sort: SortKey = "total_time",
    source_id: int | None = None,
    text: str | None = None,
    limit: int = 50,
) -> OperationList:
    """Operations ordered by total time, mean time or calls over the window.

    `text` keeps operations whose statement contains it (ignoring case). Ties are broken by
    id so the order is stable between refreshes.
    """
    query = _aggregate(window)
    if source_id is not None:
        query = query.where(Observation.source_id == source_id)
    if text:
        query = query.join(Fingerprint, Fingerprint.id == Observation.fingerprint_id).where(
            func.lower(Fingerprint.query).contains(text.lower(), autoescape=True)
        )
    total_count = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    ranked = list(
        await session.execute(
            query.order_by(_ORDER[sort].desc(), Observation.fingerprint_id).limit(limit)
        )
    )

    totals = await _window_total(session, window, None if source_id is None else [source_id])
    ids = [row.fingerprint_id for row in ranked]
    loaded = {
        fp.id: (fp, src)
        for fp, src in await session.execute(
            select(Fingerprint, DataSource)
            .join(DataSource, DataSource.id == Fingerprint.source_id)
            .where(Fingerprint.id.in_(ids))
        )
    }
    operations = []
    for row in ranked:
        fingerprint, source = loaded[row.fingerprint_id]
        operations.append(_summary(fingerprint, source, _metrics(row), totals.get(source.id, 0.0)))
    return OperationList(
        start=window.start,
        end=window.end,
        sort=sort,
        total_count=int(total_count),
        total_exec_time_ms=sum(totals.values()),
        operations=operations,
    )


async def operation_summary(
    session: AsyncSession, fingerprint: Fingerprint, source: DataSource, window: Window
) -> OperationSummary:
    """One operation's metrics over the window (zeros when it didn't run in it)."""
    row = (
        await session.execute(
            _aggregate(window).where(Observation.fingerprint_id == fingerprint.id)
        )
    ).first()
    metrics = (
        _metrics(row)
        if row is not None
        else OperationMetrics(
            calls=0,
            total_exec_time_ms=0.0,
            mean_exec_time_ms=0.0,
            rows=0,
            shared_blks_hit=0,
            shared_blks_read=0,
        )
    )
    totals = await _window_total(session, window, [source.id])
    return _summary(fingerprint, source, metrics, totals.get(source.id, 0.0))


async def operation_history(
    session: AsyncSession, fingerprint_id: int, window: Window
) -> list[HistoryPoint]:
    """Calls, time and rows per bucket (a minute or an hour), with empty buckets as zeros.

    Buckets come from date_trunc in SQL. An observation lands in the bucket its interval
    ended in. Empty buckets are filled in here so a chart shows quiet periods as zero
    instead of drawing a line across the gap.

    Minute buckets stop at the last *complete* minute (like the health strip): the current
    minute's partial total would always look like a sudden drop at the right edge. Hour
    buckets keep the current hour, since leaving it out would hide up to 59 minutes of data.
    """
    trunc = trunc_minute if window.resolution == "minute" else trunc_hour
    bucket = trunc(Observation.interval_end)
    rows = await session.execute(
        _in_window(
            select(
                bucket.label("bucket"),
                _CALLS.label("calls"),
                _TOTAL.label("total"),
                func.sum(Observation.rows).label("rows"),
            ),
            window,
        )
        .where(Observation.fingerprint_id == fingerprint_id)
        .group_by(bucket)
    )
    found = {floor_to(row.bucket, window.resolution): row for row in rows}
    points = []
    current = floor_to(window.start, window.resolution)
    last = floor_to(window.end, window.resolution)
    if window.resolution == "minute":
        last -= window.step  # the minute containing `end` isn't over
    while current <= last:
        row = found.get(current)
        calls = int(row.calls or 0) if row else 0
        total = float(row.total or 0.0) if row else 0.0
        points.append(
            HistoryPoint(
                start=current,
                calls=calls,
                total_exec_time_ms=total,
                mean_exec_time_ms=total / calls if calls else None,
                rows=int(row.rows or 0) if row else 0,
            )
        )
        current += window.step
    return points
