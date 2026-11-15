"""Read-side helpers over observations: per-minute trends for the health strip."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.workload.models import Observation
from query_pulse.workload.schemas import TrendPoint

BUCKET = timedelta(minutes=1)


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; everything is stored in UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def floor_minute(value: datetime) -> datetime:
    return as_utc(value).replace(second=0, microsecond=0)


def bucket(
    observations: list[tuple[datetime, int, float]], since: datetime, until: datetime
) -> list[TrendPoint]:
    """Sum (interval_end, calls, total_ms) into one point per minute, zeros included.

    Each observation is attributed to the minute its interval ended in. Every minute in the
    window gets a point, so a chart shows quiet periods as zero instead of joining across gaps.
    """
    calls: dict[datetime, int] = defaultdict(int)
    total: dict[datetime, float] = defaultdict(float)
    for interval_end, n, ms in observations:
        minute = floor_minute(interval_end)
        calls[minute] += n
        total[minute] += ms
    points = []
    minute = floor_minute(since)
    while minute <= floor_minute(until):
        points.append(
            TrendPoint(start=minute, calls=calls[minute], total_exec_time_ms=total[minute])
        )
        minute += BUCKET
    return points


async def trends(
    session: AsyncSession, source_ids: list[int], minutes: int, now: datetime | None = None
) -> dict[int, list[TrendPoint]]:
    """Per-minute trend for each source over the last `minutes` complete minutes.

    The current minute is left out: it isn't over, so its partial total would always look
    like a sudden drop at the right edge of a chart.
    """
    now = now or datetime.now(UTC)
    until = floor_minute(now) - BUCKET
    since = until - (minutes - 1) * BUCKET
    rows = await session.execute(
        select(
            Observation.source_id,
            Observation.interval_end,
            Observation.calls,
            Observation.total_exec_time_ms,
        ).where(
            Observation.source_id.in_(source_ids),
            Observation.interval_end >= since,
            Observation.interval_end < until + BUCKET,
        )
    )
    per_source: dict[int, list[tuple[datetime, int, float]]] = defaultdict(list)
    for source_id, interval_end, n, ms in rows:
        per_source[source_id].append((interval_end, n, ms))
    return {sid: bucket(per_source[sid], since, until) for sid in source_ids}
