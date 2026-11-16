"""One collection cycle for one source: connect, read, store, update state.

PostgreSQL: snapshot pg_stat_statements, compute deltas against the stored baseline, store
them, plan the busiest statements. MongoDB: read the profiler after a cursor and store one
observation per query shape (collector/mongo.py).

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
from pymongo.errors import OperationFailure, PyMongoError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.adapters import AdapterError, ErrorCode
from query_pulse.adapters.mongodb import MongoAdapter
from query_pulse.adapters.postgres import PostgresAdapter, StatementsReading
from query_pulse.collector import mongo
from query_pulse.collector.delta import (
    Counters,
    DeltaResult,
    Snapshot,
    StatementKey,
    compute_deltas,
)
from query_pulse.collector.models import CollectorState
from query_pulse.collector.warnings import source_warnings
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import utcnow
from query_pulse.findings import engine as findings
from query_pulse.sources import service as sources
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from query_pulse.workload import plans
from query_pulse.workload.models import (
    Fingerprint,
    Observation,
    Plan,
    RelationStat,
    ServerSample,
)

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
        mongo_adapter: MongoAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.cipher = cipher
        self.adapter = adapter or PostgresAdapter()
        self.mongo = mongo_adapter or MongoAdapter()
        # The newest command of each MongoDB read shape, per source, for explain. Process
        # memory only: never written to the app database or logged (ADR 0010).
        self.samples: dict[int, mongo.SampleCache] = {}

    async def collect(self, source_id: int) -> CycleOutcome:
        """Run one cycle. Never raises (except cancellation)."""
        async with self.sessionmaker() as session:
            source = await session.get(DataSource, source_id)
            if source is None or not source.enabled:
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
                    if source.engine is SourceEngine.MONGODB:
                        outcome = await self._collect_mongo(session, source, state, now)
                    else:
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
            except OperationFailure as exc:
                # Only the code: MongoDB's message can quote a query's values.
                message = f"A collection command failed (MongoDB error {exc.code})."
                outcome = self._record_failure(source, state, message, ErrorCode.UNKNOWN, now)
            except PyMongoError as exc:
                message = (
                    f"Lost the connection to MongoDB during collection ({type(exc).__name__})."
                )
                outcome = self._record_failure(source, state, message, ErrorCode.UNREACHABLE, now)
            except Exception:  # noqa: BLE001 - a bug must not kill the source's loop
                logger.exception("collector: unexpected error for source=%s", source.name)
                message = "Unexpected collector error; see the collector logs."
                outcome = self._record_failure(source, state, message, ErrorCode.UNKNOWN, now)
            await session.commit()
        # Only after a real collection: a skipped (disabled) source has no new evidence, and
        # judging its ageing data would "resolve" findings that aren't fixed.
        if outcome.ok and not outcome.skipped:
            await self.evaluate_rules(source_id)
        return outcome

    async def evaluate_rules(self, source_id: int, force: bool = False) -> bool:
        """Run the findings rules for a source, about every `rules_interval_seconds`.

        Runs after the cycle has committed, in its own session: the rules read what the
        cycle stored, and a failure here can never undo or fail the collection itself.
        Returns whether the rules ran.
        """
        async with self.sessionmaker() as session:
            state = await session.get(CollectorState, source_id)
            now = utcnow()
            # 10% slack: cycles are jittered by ±10%, and a strict check would skip every
            # other cycle when the two intervals are equal.
            interval = self.settings.rules_interval_seconds * 0.9
            if state is None or (not force and not _due(state.last_rules_at, interval, now)):
                return False
            try:
                async with asyncio.timeout(self.settings.collector_cycle_timeout_seconds):
                    await findings.evaluate_source(session, source_id, self.settings, now)
                state.last_rules_at = now
                await session.commit()
            except Exception:  # noqa: BLE001 - findings must never break collection
                logger.exception("findings: evaluation failed for source_id=%s", source_id)
                await session.rollback()
                return False
        return True

    async def _collect(
        self, session: AsyncSession, source: DataSource, state: CollectorState, now: datetime
    ) -> CycleOutcome:
        params = sources.params_for(
            sources.connection_fields(source),
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
            if plans.plans_available(source):
                await self._capture_plans(session, source, state, conn, now)

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

        self._record_success(source, state, now)
        logger.info(
            "collector: source=%s observations=%d discarded=%s",
            source.name,
            stored,
            result.discarded,
        )
        return CycleOutcome(ok=True, observations=stored, discarded=result.discarded)

    async def _collect_mongo(
        self, session: AsyncSession, source: DataSource, state: CollectorState, now: datetime
    ) -> CycleOutcome:
        params = sources.params_for(
            sources.connection_fields(source),
            sources.stored_password(source, self.cipher),
            self.settings.source_connect_timeout_seconds,
        )
        if _due(
            state.last_capability_check_at, self.settings.collector_capability_check_seconds, now
        ):
            sources.record_success(source, await self.mongo.probe(params))
            state.last_capability_check_at = now

        client = await self.mongo.connect(params)
        try:
            # Someone turned the profiler on or off since the last check: re-check now, so
            # the checklist (and the explorer's "profiling is off" state) is current.
            try:
                level: int | None = await self.mongo.profiling_level(client, source.database)
            except OperationFailure:
                level = None
            profiler = next((c for c in source.capabilities if c.feature == "profiler"), None)
            if level is not None and profiler is not None and (level > 0) != profiler.available:
                sources.record_success(source, await self.mongo.probe(params))
                state.last_capability_check_at = now

            target = (source.host, str(source.port), source.database, source.username)
            cache = self.samples.get(source.id)
            if cache is None or cache.target != target:
                # New source, or it now points elsewhere: start with no commands.
                cache = mongo.SampleCache(self.settings.collector_sample_cache_size, target)
                self.samples[source.id] = cache
            result, cursor = await mongo.collect_mongo(
                session,
                source,
                state.snapshot_at,
                state.profile_cursor,
                client,
                self.mongo,
                cache,
                self.settings,
                now,
            )
            state.profile_cursor = cursor
            explain = next((c for c in source.capabilities if c.feature == "explain"), None)
            if explain is not None and explain.available:
                full = _due(state.last_plan_capture_at, self.settings.collector_plan_seconds, now)
                await mongo.capture_plans(
                    session, source, full, client, self.mongo, cache, self.settings, now
                )
                if full:
                    state.last_plan_capture_at = now
        finally:
            await client.close()

        state.snapshot_at = now
        if result.overflowed:
            state.last_profile_overflow_at = now
        self._record_success(source, state, now)
        logger.info(
            "collector: source=%s observations=%d profiler_read=%s",
            source.name,
            result.observations,
            result.profiler_read,
        )
        return CycleOutcome(ok=True, observations=result.observations)

    @staticmethod
    def _record_success(source: DataSource, state: CollectorState, now: datetime) -> None:
        state.consecutive_failures = 0
        state.last_success_at = now
        state.last_error = None
        source.last_error = None
        all_available = all(c.available for c in source.capabilities)
        source.status = (
            SourceStatus.HEALTHY
            if all_available and not source_warnings(state, source.engine.value, now)
            else SourceStatus.DEGRADED
        )

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
        # Matching on queryid selects PostgreSQL rows only, whose identity columns are set.
        existing = {
            StatementKey(
                int(f.dbid or 0), int(f.userid or 0), int(f.queryid or 0), bool(f.toplevel)
            ): f
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

    async def _capture_plans(
        self,
        session: AsyncSession,
        source: DataSource,
        state: CollectorState,
        conn: plans.Connection,
        now: datetime,
    ) -> None:
        """Plan the source's busiest operations of the last hour.

        A full re-capture runs every `collector_plan_seconds`; in between, only operations
        that have no plan yet are planned, so a new hot query gets its plan within a cycle.
        Planning is bounded to a third of the cycle timeout: running out of time just leaves
        the rest for the next cycle instead of failing the whole collection.
        """
        top = await plans.top_fingerprints(
            session, source.id, now - timedelta(hours=1), self.settings.collector_plan_top_n
        )
        full = _due(state.last_plan_capture_at, self.settings.collector_plan_seconds, now)
        if not full:
            done = await plans.planned_ids(session, [f.id for f in top])
            top = [f for f in top if f.id not in done]
        # The budget covers only the EXPLAIN calls: cancelling one of our own app-database
        # writes halfway could leave the session unusable for the rest of the cycle.
        deadline = asyncio.get_running_loop().time() + (
            self.settings.collector_cycle_timeout_seconds / 3
        )
        for planned, fingerprint in enumerate(top):
            try:
                async with asyncio.timeout_at(deadline):
                    plan, error = await plans.explain(
                        self.adapter, conn, fingerprint, source.username
                    )
            except TimeoutError:
                logger.warning(
                    "collector: source=%s planning stopped after %d of %d (time budget)",
                    source.name,
                    planned,
                    len(top),
                )
                return
            await plans.record_plan(session, fingerprint, plan, error, now)
        if full:
            state.last_plan_capture_at = now

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
        """Delete observations, relation samples and plans older than the retention period."""
        cutoff = utcnow() - timedelta(days=self.settings.retention_days)
        async with self.sessionmaker() as session:
            removed = 0
            for statement in (
                delete(Observation).where(Observation.interval_end < cutoff),
                delete(RelationStat).where(RelationStat.captured_at < cutoff),
                delete(ServerSample).where(ServerSample.captured_at < cutoff),
                # A plan still being re-checked keeps a recent checked_at, so it stays.
                delete(Plan).where(Plan.checked_at < cutoff),
            ):
                result = await session.execute(statement)
                removed += int(getattr(result, "rowcount", 0) or 0)
            await session.commit()
        logger.info("collector: retention removed %d rows older than %s", removed, cutoff.date())
        return removed
