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


def profile_overflow_warning(
    state: CollectorState | None, now: datetime | None = None
) -> str | None:
    """MongoDB: the capped system.profile overwrote entries before they were read."""
    if state is None or state.last_profile_overflow_at is None:
        return None
    now = now or datetime.now(UTC)
    last = state.last_profile_overflow_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if now - last > EVICTION_WARNING_WINDOW:
        return None
    return (
        "MongoDB's system.profile collection is full and overwrote operations before Query "
        "Pulse read them, so some activity wasn't counted. Make it larger: with profiling "
        "off, drop system.profile and recreate it capped with a bigger size, then turn "
        "profiling back on."
    )


def source_warnings(
    state: CollectorState | None, engine: str, now: datetime | None = None
) -> list[str]:
    """Everything that makes a source's collected results incomplete right now."""
    found = (
        eviction_warning(state, now)
        if engine == "postgres"
        else profile_overflow_warning(state, now)
    )
    return [found] if found else []
