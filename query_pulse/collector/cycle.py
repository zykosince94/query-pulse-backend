"""One collection cycle for one source: connect, snapshot, compute deltas, store, update state.

Every cycle opens its own database session and its own monitoring connection, and never
raises: failures are recorded on the source (status `failing`, `last_error`) so the
supervisor can back off and try again.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.adapters import AdapterError, ErrorCode
from query_pulse.adapters.postgres import PostgresAdapter, StatementsReading
from query_pulse.collector.delta import (
    Counters,
    DeltaResult,
    Snapshot,
    StatementKey,
    compute_deltas,
)
from query_pulse.collector.models import CollectorState
from query_pulse.collector.warnings import eviction_warning
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import utcnow
from query_pulse.sources import service as sources
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from query_pulse.sources.schemas import ConnectionFields
from query_pulse.workload.models import Fingerprint, Observation, RelationStat

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleOutcome:
    ok: bool
    consecutive_failures: int = 0
    observations: int = 0
    discarded: str | None = None
    skipped: bool = False


def _due(last: datetime | None, every_seconds: float, now: datetime) -> bool:
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return (now - last).total_seconds() >= every_seconds


def _connection_fields(source: DataSource) -> ConnectionFields:
    return ConnectionFields(
        engine=source.engine,
        host=source.host,
        port=source.port,
        database=source.database,
        username=source.username,
        tls_mode=source.tls_mode,
    )


def _snapshot(reading: StatementsReading, taken_at: datetime) -> Snapshot:
    return Snapshot(
        taken_at=taken_at,
        stats_reset=reading.stats_reset,
        dealloc=reading.dealloc,
        statements={
            StatementKey(r.dbid, r.userid, r.queryid, r.toplevel): Counters(
                r.calls, r.total_exec_time, r.rows, r.shared_blks_hit, r.shared_blks_read
            )
            for r in reading.statements
        },
    )


class Collector:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        cipher: CredentialCipher,
        adapter: PostgresAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.cipher = cipher
        self.adapter = adapter or PostgresAdapter()

    async def collect(self, source_id: int) -> CycleOutcome:
        """Run one cycle. Never raises (except cancellation)."""
        async with self.sessionmaker() as session:
            source = await session.get(DataSource, source_id)
            if source is None or not source.enabled or source.engine is not SourceEngine.POSTGRES:
                return CycleOutcome(ok=True, skipped=True)
            state = await session.get(CollectorState, source_id)
            if state is None:
                state = CollectorState(
                    source_id=source_id, consecutive_failures=0, evicted_statements=0
                )
                session.add(state)

            now = utcnow()
            state.last_attempt_at = now
            try:
                async with asyncio.timeout(self.settings.collector_cycle_timeout_seconds):
                    outcome = await self._collect(session, source, state, now)
            except AdapterError as error:
                outcome = self._record_failure(source, state, error.message, error.code, now)
            except TimeoutError:
                message = (
                    f"Collection didn't finish within "
                    f"{self.settings.collector_cycle_timeout_seconds:g}s."
                )
                outcome = self._record_failure(source, state, message, ErrorCode.TIMEOUT, now)
            except psycopg.Error as exc:
                message = f"Collection query failed: {str(exc).strip().splitlines()[0]}"
                outcome = self._record_failure(source, state, message, ErrorCode.UNKNOWN, now)
            except Exception:  # noqa: BLE001 - a bug must not kill the source's loop
                logger.exception("collector: unexpected error for source=%s", source.name)
                message = "Unexpected collector error; see the collector logs."
                outcome = self._record_failure(source, state, message, ErrorCode.UNKNOWN, now)
            await session.commit()
            return outcome

    async def _collect(
        self, session: AsyncSession, source: DataSource, state: CollectorState, now: datetime
    ) -> CycleOutcome:
        params = sources.params_for(
            _connection_fields(source),
            sources.stored_password(source, self.cipher),
            self.settings.source_connect_timeout_seconds,
        )

        # Capability re-checks (e.g. someone enabled an extension later) on their own schedule.
        if _due(
            state.last_capability_check_at, self.settings.collector_capability_check_seconds, now
        ):
            checked = await self.adapter.probe(params)
            sources.record_success(source, checked)
            state.last_capability_check_at = now

        conn = await self.adapter.connect(params)
        async with conn:
            reading = await self.adapter.read_statements(conn)
            relations = (
                await self.adapter.read_relations(conn)
                if _due(
                    state.last_relation_stats_at,
                    self.settings.collector_relation_stats_seconds,
                    now,
                )
                else None
            )

        current = _snapshot(reading, now)
        previous = Snapshot.from_json(state.snapshot) if state.snapshot else None
        result = compute_deltas(previous, current)
        stored = await self._store(session, source, reading, result)

        # The current snapshot becomes the baseline for the next cycle, even when this
        # interval was discarded (after a reset, counting restarts from here).
        state.snapshot = current.to_json()
        state.snapshot_at = now
        state.stats_reset = reading.stats_reset
        state.dealloc = reading.dealloc
        if result.evicted:
            state.last_eviction_at = now
            state.evicted_statements = (state.evicted_statements or 0) + result.evicted
            logger.warning(
                "collector: source=%s evicted=%d (pg_stat_statements.max too small)",
                source.name,
                result.evicted,
            )
        if relations is not None:
            session.add_all(
                RelationStat(source_id=source.id, captured_at=now, **vars(r)) for r in relations
            )
            state.last_relation_stats_at = now

        state.consecutive_failures = 0
        state.last_success_at = now
        state.last_error = None
        source.last_error = None
        all_available = all(c.available for c in source.capabilities)
        source.status = (
            SourceStatus.HEALTHY
            if all_available and eviction_warning(state, now) is None
            else SourceStatus.DEGRADED
        )
        logger.info(
            "collector: source=%s observations=%d discarded=%s",
            source.name,
            stored,
            result.discarded,
        )
        return CycleOutcome(ok=True, observations=stored, discarded=result.discarded)

    async def _store(
        self,
        session: AsyncSession,
        source: DataSource,
        reading: StatementsReading,
        result: DeltaResult,
    ) -> int:
        """Upsert fingerprints for active statements and insert their observations."""
        if not result.deltas or result.interval_start is None:
            return 0
        texts = {
            StatementKey(r.dbid, r.userid, r.queryid, r.toplevel): r.query
            for r in reading.statements
        }
        existing = {
            StatementKey(f.dbid, f.userid, f.queryid, f.toplevel): f
            for f in await session.scalars(
                select(Fingerprint).where(
                    Fingerprint.source_id == source.id,
                    Fingerprint.queryid.in_({d.key.queryid for d in result.deltas}),
                )
            )
        }
        # Pass 1: make sure every active statement has a fingerprint row.
        for delta in result.deltas:
            if delta.key not in existing:
                existing[delta.key] = Fingerprint(
                    source_id=source.id,
                    dbid=delta.key.dbid,
                    userid=delta.key.userid,
                    queryid=delta.key.queryid,
                    toplevel=delta.key.toplevel,
                    query=texts.get(delta.key, ""),
                    first_seen_at=result.interval_end,
                )
                session.add(existing[delta.key])
            existing[delta.key].last_seen_at = result.interval_end
        await session.flush()  # one round trip assigns ids to all new fingerprints

        # Pass 2: one observation per active statement for this interval.
        for delta in result.deltas:
            fingerprint = existing[delta.key]
            c = delta.counters
            session.add(
                Observation(
                    fingerprint_id=fingerprint.id,
                    source_id=source.id,
                    interval_start=result.interval_start,
                    interval_end=result.interval_end,
                    calls=c.calls,
                    total_exec_time_ms=c.total_exec_time,
                    mean_exec_time_ms=delta.mean_exec_time,
                    rows=c.rows,
                    shared_blks_hit=c.shared_blks_hit,
                    shared_blks_read=c.shared_blks_read,
                )
            )
        return len(result.deltas)

    @staticmethod
    def _record_failure(
        source: DataSource, state: CollectorState, message: str, code: ErrorCode, now: datetime
    ) -> CycleOutcome:
        state.consecutive_failures = (state.consecutive_failures or 0) + 1
        state.last_error = message
        source.status = SourceStatus.FAILING
        source.last_error = message
        source.last_checked_at = now
        logger.warning(
            "collector: source=%s failed code=%s failures=%d",
            source.name,
            code.value,
            state.consecutive_failures,
        )
        return CycleOutcome(ok=False, consecutive_failures=state.consecutive_failures)

    async def purge_old_data(self) -> int:
        """Delete observations and relation samples older than the retention period."""
        cutoff = utcnow() - timedelta(days=self.settings.retention_days)
        async with self.sessionmaker() as session:
            removed = 0
            for statement in (
                delete(Observation).where(Observation.interval_end < cutoff),
                delete(RelationStat).where(RelationStat.captured_at < cutoff),
            ):
                result = await session.execute(statement)
                removed += int(getattr(result, "rowcount", 0) or 0)
            await session.commit()
        logger.info("collector: retention removed %d rows older than %s", removed, cutoff.date())
        return removed
