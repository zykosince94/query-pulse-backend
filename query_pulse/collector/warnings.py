"""Warnings derived from collector state (shared by the collector and the API)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from query_pulse.collector.models import CollectorState

# How long an eviction keeps the source flagged: long enough to notice, short enough to clear
# once pg_stat_statements.max has been raised.
EVICTION_WARNING_WINDOW = timedelta(hours=1)


def eviction_warning(state: CollectorState | None, now: datetime | None = None) -> str | None:
    """The warning to show while evictions are recent, or None."""
    if state is None or state.last_eviction_at is None:
        return None
    now = now or datetime.now(UTC)
    last = state.last_eviction_at
    if last.tzinfo is None:  # SQLite returns naive datetimes; they're stored as UTC
        last = last.replace(tzinfo=UTC)
    if now - last > EVICTION_WARNING_WINDOW:
        return None
    return (
        f"pg_stat_statements evicted {state.evicted_statements} statement(s) because its table "
        "is full, so some activity wasn't recorded. Raise pg_stat_statements.max (and restart "
        "PostgreSQL)."
    )
