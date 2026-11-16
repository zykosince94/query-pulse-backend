"""One collection cycle for a MongoDB source.

    server time ─┐
    serverStatus ┼─ one server sample (opcounters, connections, cache)
    profiler ────┴─ entries after the cursor → shapes → one observation per shape
                    (and the newest command of each read shape, kept in memory only)
    explain ────── the busiest shapes' plans, from those in-memory commands, redacted

Unlike pg_stat_statements' running totals, the profiler logs each slow operation once, with
its own timing. So there's no delta engine here: a cycle reads the entries after a cursor
(a server timestamp kept in collector_states) and sums them per shape. The first cycle only
sets the cursor, like the first PostgreSQL snapshot only sets the baseline.

The cursor stops a second short of the server's clock: an operation that finished a moment
before the read may not be in system.profile yet, and reading up to "now" would skip it
forever. Waiting a second costs nothing; it's simply counted in the next cycle.

No query value is persisted (ADR 0010). Profiler entries hold the real commands; this
module keeps their shapes and counters, and holds the newest command of each read shape in
`SampleCache` (process memory, never written or logged) only so the shape can be explained.
After a collector restart a shape's plan can be captured again once it's seen again.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pymongo.errors import OperationFailure
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.adapters.mongodb import (
    UNAUTHORIZED,
    MongoAdapter,
    MongoClient,
    ProfileEntry,
)
from query_pulse.config import Settings
from query_pulse.sources.models import DataSource
from query_pulse.workload import plans
from query_pulse.workload.models import Fingerprint, Observation, ServerSample
from query_pulse.workload.mongo_plan import MongoPlanFormatError, redact
from query_pulse.workload.mongo_shape import NotExplainableError, Shape, explain_command, shape_of
from query_pulse.workload.service import as_utc

logger = logging.getLogger(__name__)

# Read the profiler up to this long before the server's "now" (see the module docstring).
PROFILE_SETTLE = timedelta(seconds=1)


@dataclass
class Sample:
    """The newest command seen for one shape. Memory only: it holds real values."""

    shape: Shape
    command: dict[str, Any] = field(repr=False)
    seen_at: datetime


class SampleCache:
    """The newest command per read shape for one source, bounded (least recently seen go)."""

    def __init__(self, size: int, target: tuple[str, ...] = ()) -> None:
        self.size = size
        # The server the commands were profiled on. Commands hold that server's values, so
        # they must never be explained on another one (a source re-targeted to a new host).
        self.target = target
        self._items: OrderedDict[str, Sample] = OrderedDict()

    def put(self, shape: Shape, command: dict[str, Any], seen_at: datetime) -> None:
        self._items[shape.hash] = Sample(shape, command, seen_at)
        self._items.move_to_end(shape.hash)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def get(self, shape_hash: str | None) -> Sample | None:
        return self._items.get(shape_hash) if shape_hash else None

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class ShapeTotals:
    """What one shape did in one cycle: sums over its profiled operations."""

    shape: Shape
    calls: int = 0
    millis: float = 0.0
    returned: int = 0
    docs_examined: int = 0
    keys_examined: int = 0
    last_command: dict[str, Any] = field(default_factory=dict, repr=False)
    last_ts: datetime | None = None


def group_profile(entries: list[ProfileEntry]) -> dict[str, ShapeTotals]:
    """Sum profiler entries per shape. Entries that aren't fingerprinted are skipped."""
    totals: dict[str, ShapeTotals] = {}
    for entry in entries:
        shape = shape_of(entry.op, entry.ns, entry.command)
        if shape is None:
            continue
        item = totals.get(shape.hash)
        if item is None:
            item = totals[shape.hash] = ShapeTotals(shape)
        item.calls += 1
        item.millis += entry.millis
        item.returned += entry.nreturned
        item.docs_examined += entry.docs_examined
        item.keys_examined += entry.keys_examined
        item.last_command = entry.command
        item.last_ts = entry.ts
    return totals


@dataclass(frozen=True)
class MongoCycleResult:
    observations: int
    overflowed: bool
    profiler_read: bool


def _capability(source: DataSource, feature: str) -> bool | None:
    found = next((c for c in source.capabilities if c.feature == feature), None)
    return None if found is None else found.available


async def store_shapes(
    session: AsyncSession,
    source: DataSource,
    totals: dict[str, ShapeTotals],
    interval_start: datetime,
    interval_end: datetime,
) -> int:
    """Upsert a fingerprint per shape and insert one observation per shape."""
    if not totals:
        return 0
    existing = {
        f.shape_hash: f
        for f in await session.scalars(
            select(Fingerprint).where(
                Fingerprint.source_id == source.id, Fingerprint.shape_hash.in_(totals.keys())
            )
        )
    }
    for digest, item in totals.items():
        if digest not in existing:
            existing[digest] = Fingerprint(
                source_id=source.id,
                engine="mongodb",
                shape_hash=digest,
                namespace=item.shape.namespace,
                command=item.shape.command,
                query=item.shape.text,
                first_seen_at=interval_end,
            )
            session.add(existing[digest])
        existing[digest].last_seen_at = interval_end
    await session.flush()
    for digest, item in totals.items():
        session.add(
            Observation(
                fingerprint_id=existing[digest].id,
                source_id=source.id,
                interval_start=interval_start,
                interval_end=interval_end,
                calls=item.calls,
                total_exec_time_ms=item.millis,
                mean_exec_time_ms=item.millis / item.calls if item.calls else 0.0,
                rows=item.returned,
                shared_blks_hit=0,
                shared_blks_read=0,
                keys_examined=item.keys_examined,
                docs_examined=item.docs_examined,
            )
        )
    return len(totals)


async def collect_mongo(
    session: AsyncSession,
    source: DataSource,
    previous_end: datetime | None,
    cursor: datetime | None,
    client: MongoClient,
    adapter: MongoAdapter,
    cache: SampleCache,
    settings: Settings,
    now: datetime,
) -> tuple[MongoCycleResult, datetime | None]:
    """Sample the server and read the profiler. Returns the result and the new cursor."""
    # SQLite (unit tests) returns naive datetimes; everything is stored in UTC.
    cursor = as_utc(cursor) if cursor is not None else None
    previous_end = as_utc(previous_end) if previous_end is not None else None
    server_now = await adapter.server_time(client)

    if _capability(source, "server_status") is not False:
        try:
            status = await adapter.server_status(client)
        except OperationFailure as exc:
            if exc.code not in UNAUTHORIZED:
                raise
        else:
            session.add(ServerSample(source_id=source.id, captured_at=now, **vars(status)))

    until = server_now - PROFILE_SETTLE
    if cursor is None:
        # First cycle: start counting from here (an older backlog has no interval to go in).
        return MongoCycleResult(0, overflowed=False, profiler_read=False), until

    try:
        reading = await adapter.read_profile(
            client, source.database, cursor, until, settings.collector_profile_max_entries
        )
    except OperationFailure as exc:
        if exc.code not in UNAUTHORIZED:
            raise
        # No read on the database: the capability checklist already says how to fix it.
        return MongoCycleResult(0, overflowed=False, profiler_read=False), cursor

    if reading.truncated and reading.entries:
        # More waiting than one cycle reads: continue after the last entry next time.
        until = reading.entries[-1].ts
    totals = group_profile(reading.entries)
    for item in totals.values():
        if item.shape.is_read and item.last_ts is not None:
            cache.put(item.shape, item.last_command, item.last_ts)
    start = previous_end or now - timedelta(seconds=settings.collector_interval_seconds)
    stored = await store_shapes(session, source, totals, start, now)
    if reading.overflowed:
        logger.warning(
            "collector: source=%s system.profile overwrote entries before they were read",
            source.name,
        )
    return MongoCycleResult(stored, reading.overflowed, profiler_read=True), until


def _explain_error(exc: OperationFailure) -> str:
    """An explain failure in words. MongoDB's own message can quote the query's values, so
    only the error code and name are kept."""
    name = (exc.details or {}).get("codeName") if isinstance(exc.details, dict) else None
    if exc.code in UNAUTHORIZED:
        return (
            "The monitoring user can't explain this operation. Explain needs read on the "
            "collection's database."
        )
    return f"explain failed (MongoDB error {exc.code}{f' {name}' if name else ''})."


async def capture_plans(
    session: AsyncSession,
    source: DataSource,
    full: bool,
    client: MongoClient,
    adapter: MongoAdapter,
    cache: SampleCache,
    settings: Settings,
    now: datetime,
) -> None:
    """Explain the busiest shapes of the last hour that have a command in memory.

    Same schedule as PostgreSQL: all of the top shapes every `collector_plan_seconds`,
    otherwise only those without a plan yet. A third of the cycle timeout at most.
    """
    top = await plans.top_fingerprints(
        session, source.id, now - timedelta(hours=1), settings.collector_plan_top_n
    )
    if not full:
        done = await plans.planned_ids(session, [f.id for f in top])
        top = [f for f in top if f.id not in done]
    deadline = asyncio.get_running_loop().time() + settings.collector_cycle_timeout_seconds / 3
    for fingerprint in top:
        sample = cache.get(fingerprint.shape_hash)
        if sample is None:
            continue  # not seen since the collector started: no command to explain
        plan: list[Any] | None = None
        error: str | None = None
        try:
            command = explain_command(sample.shape, sample.command)
            async with asyncio.timeout_at(deadline):
                plan = [redact(await adapter.explain(client, sample.shape.database, command))]
        except NotExplainableError as exc:
            error = str(exc)
        except OperationFailure as exc:
            error = _explain_error(exc)
        except MongoPlanFormatError as exc:
            error = f"Unreadable explain output: {exc}"
        except TimeoutError:
            logger.warning("collector: source=%s planning stopped (time budget)", source.name)
            return
        await plans.record_plan(session, fingerprint, plan, error, now)
