from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

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
