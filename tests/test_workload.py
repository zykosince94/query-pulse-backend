"""Collector plumbing without real databases: backoff, supervisor, retention, read API."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.collector.backoff import jittered, next_delay
from query_pulse.collector.cycle import Collector, CycleOutcome
from query_pulse.collector.models import CollectorHeartbeat, CollectorState
from query_pulse.collector.supervisor import Supervisor
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from query_pulse.workload.models import Fingerprint, Observation, RelationStat
from query_pulse.workload.service import bucket

Maker = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 16, 12, 30, 20, tzinfo=UTC)


# --- backoff ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(0, 60), (1, 60), (2, 120), (3, 240), (4, 300), (10, 300)],
)
def test_backoff_doubles_up_to_the_cap(failures: int, expected: float) -> None:
    assert next_delay(60, failures, max_backoff=300) == expected


def test_jitter_stays_within_ten_percent() -> None:
    rng = random.Random(7)
    samples = [jittered(100, rng) for _ in range(1000)]
    assert min(samples) >= 90 and max(samples) <= 110
    assert len({round(s, 3) for s in samples}) > 900, "actually spread out"


# --- trend bucketing -------------------------------------------------------------------------


def test_bucket_sums_per_minute_and_fills_gaps_with_zero() -> None:
    since = NOW - timedelta(minutes=4)
    observations = [
        (NOW - timedelta(minutes=4, seconds=-5), 10, 20.0),  # 12:26:25
        (NOW - timedelta(minutes=4, seconds=-30), 5, 10.0),  # 12:26:50, same minute
        (NOW - timedelta(minutes=1), 7, 3.5),  # 12:29
    ]
    points = bucket(observations, since, NOW)
    assert [p.start.strftime("%H:%M") for p in points] == [
        "12:26",
        "12:27",
        "12:28",
        "12:29",
        "12:30",
    ]
    assert [p.calls for p in points] == [15, 0, 0, 7, 0]
    assert points[0].total_exec_time_ms == pytest.approx(30.0)


def test_trend_excludes_the_unfinished_current_minute() -> None:
    import asyncio as _asyncio

    from query_pulse.workload import service

    class FakeSession:
        async def execute(self, statement: object) -> list[tuple[int, datetime, int, float]]:
            return [
                (1, NOW - timedelta(minutes=1), 5, 1.0),  # 12:29: complete
                (1, NOW, 99, 9.0),  # 12:30:20: the current, unfinished minute
            ]

    points = _asyncio.run(service.trends(FakeSession(), [1], 3, now=NOW))[1]  # type: ignore[arg-type]
    assert [p.start.strftime("%H:%M") for p in points] == ["12:27", "12:28", "12:29"]
    assert [p.calls for p in points] == [0, 0, 5]


def test_bucket_handles_naive_datetimes_from_sqlite() -> None:
    naive = NOW.replace(tzinfo=None)
    points = bucket([(naive, 3, 1.0)], NOW - timedelta(minutes=1), NOW)
    assert points[-1].calls == 3


# --- database fixtures for collector-side tests ----------------------------------------------


@pytest.fixture
def sessionmaker(database_url: str) -> Iterator[Maker]:
    engine = create_engine(database_url)
    yield create_sessionmaker(engine)
    asyncio.run(engine.dispose())


def add_source(sessionmaker: Maker, **overrides: Any) -> int:
    async def _add() -> int:
        async with sessionmaker() as session:
            source = DataSource(
                name=overrides.pop("name", "Orders DB"),
                engine=overrides.pop("engine", SourceEngine.POSTGRES),
                host="db.example",
                port=5432,
                database="shop",
                username="monitor",
                tls_mode="disable",
                enabled=overrides.pop("enabled", True),
                description="",
                status=SourceStatus.HEALTHY,
                **overrides,
            )
            session.add(source)
            await session.commit()
            return source.id

    return asyncio.run(_add())


# --- supervisor ------------------------------------------------------------------------------


class ScriptedCollector:
    """Fails a given number of times, then succeeds; records each call."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.samples: dict[int, object] = {}

    async def collect(self, source_id: int) -> CycleOutcome:
        self.calls += 1
        if self.calls <= self.failures:
            return CycleOutcome(ok=False, consecutive_failures=self.calls)
        return CycleOutcome(ok=True)


def test_source_loop_keeps_going_after_failures_and_recovers(settings: Settings) -> None:
    fast = settings.model_copy(
        update={"collector_interval_seconds": 0.01, "collector_max_backoff_seconds": 0.05}
    )
    collector = ScriptedCollector(failures=3)
    supervisor = Supervisor(fast, cast(Maker, None), cast(Collector, collector))

    async def run_briefly() -> None:
        task = asyncio.create_task(supervisor.source_loop(1, random.Random(1)))
        await asyncio.sleep(0.6)
        task.cancel()

    asyncio.run(run_briefly())
    assert collector.calls > 4, "kept polling after three failures and after recovering"


def test_reconcile_starts_and_stops_tasks(settings: Settings, sessionmaker: Maker) -> None:
    first = add_source(sessionmaker, name="First")
    mongo = add_source(sessionmaker, name="Mongo", engine=SourceEngine.MONGODB)
    add_source(sessionmaker, name="Disabled", enabled=False)

    async def scenario() -> tuple[set[int], set[int]]:
        supervisor = Supervisor(settings, sessionmaker, cast(Collector, ScriptedCollector(0)))
        await supervisor.reconcile()
        started = set(supervisor.tasks)
        async with sessionmaker() as session:
            for source_id in (first, mongo):
                source = await session.get(DataSource, source_id)
                assert source is not None
                source.enabled = False
            await session.commit()
        await supervisor.reconcile()
        remaining = set(supervisor.tasks)
        for task in supervisor.tasks.values():
            task.cancel()
        return started, remaining

    started, remaining = asyncio.run(scenario())
    assert started == {first, mongo}  # both engines are collected (Phase 6)
    assert remaining == set()


def test_heartbeat_is_written_and_triggers_retention(
    settings: Settings, sessionmaker: Maker
) -> None:
    collector = Collector(settings, sessionmaker, CredentialCipher(settings.fernet_keys))

    async def beat() -> CollectorHeartbeat:
        await Supervisor(settings, sessionmaker, collector).heartbeat()
        async with sessionmaker() as session:
            row = await session.get(CollectorHeartbeat, 1)
            assert row is not None
            return row

    row = asyncio.run(beat())
    assert row.beat_at is not None and row.last_retention_at is not None


def test_retention_deletes_only_old_rows(settings: Settings, sessionmaker: Maker) -> None:
    source_id = add_source(sessionmaker)
    old = datetime.now(UTC) - timedelta(days=settings.retention_days + 1)
    recent = datetime.now(UTC) - timedelta(hours=1)

    async def scenario() -> tuple[int, int, int]:
        async with sessionmaker() as session:
            fp = Fingerprint(
                source_id=source_id, dbid=1, userid=1, queryid=1, toplevel=True, query="SELECT 1"
            )
            session.add(fp)
            await session.flush()
            for when in (old, recent):
                session.add(
                    Observation(
                        fingerprint_id=fp.id,
                        source_id=source_id,
                        interval_start=when - timedelta(minutes=1),
                        interval_end=when,
                        calls=1,
                        total_exec_time_ms=1.0,
                        mean_exec_time_ms=1.0,
                        rows=1,
                        shared_blks_hit=0,
                        shared_blks_read=0,
                    )
                )
                session.add(
                    RelationStat(
                        source_id=source_id,
                        captured_at=when,
                        kind="table",
                        schema_name="public",
                        table_name="orders",
                        size_bytes=1,
                    )
                )
            await session.commit()
        removed = await Collector(
            settings, sessionmaker, CredentialCipher(settings.fernet_keys)
        ).purge_old_data()
        async with sessionmaker() as session:
            from sqlalchemy import func, select

            observations = await session.scalar(select(func.count()).select_from(Observation))
            relations = await session.scalar(select(func.count()).select_from(RelationStat))
        return removed, int(observations or 0), int(relations or 0)

    assert asyncio.run(scenario()) == (2, 1, 1)


# --- read API --------------------------------------------------------------------------------


def test_collector_status_reflects_the_heartbeat(
    login: Callable[[str], TestClient],
    sessionmaker: Maker,
) -> None:
    client = login("demo")
    assert client.get("/api/collector/status").json()["status"] == "not_running"

    async def beat(at: datetime) -> None:
        async with sessionmaker() as session:
            row = await session.get(CollectorHeartbeat, 1)
            if row is None:
                session.add(CollectorHeartbeat(id=1, started_at=at, beat_at=at, host="test"))
            else:
                row.beat_at = at
            await session.commit()

    asyncio.run(beat(datetime.now(UTC)))
    assert client.get("/api/collector/status").json()["status"] == "running"
    asyncio.run(beat(datetime.now(UTC) - timedelta(minutes=10)))
    assert client.get("/api/collector/status").json()["status"] == "not_running"


def test_trend_and_health_endpoints(
    login: Callable[[str], TestClient], sessionmaker: Maker
) -> None:
    source_id = add_source(sessionmaker)

    async def seed() -> None:
        async with sessionmaker() as session:
            fp = Fingerprint(
                source_id=source_id, dbid=1, userid=1, queryid=9, toplevel=True, query="SELECT 9"
            )
            session.add(fp)
            await session.flush()
            # 90 s ago: always inside a complete minute (the current minute is excluded).
            end = datetime.now(UTC) - timedelta(seconds=90)
            session.add(
                Observation(
                    fingerprint_id=fp.id,
                    source_id=source_id,
                    interval_start=end - timedelta(minutes=1),
                    interval_end=end,
                    calls=42,
                    total_exec_time_ms=84.0,
                    mean_exec_time_ms=2.0,
                    rows=42,
                    shared_blks_hit=0,
                    shared_blks_read=0,
                )
            )
            await session.commit()

    asyncio.run(seed())
    client = login("demo")
    trend = client.get(f"/api/sources/{source_id}/trend?minutes=15").json()
    assert trend["bucket_seconds"] == 60
    assert len(trend["points"]) == 15
    assert sum(p["calls"] for p in trend["points"]) == 42

    [health] = client.get("/api/workload/health?minutes=15").json()
    assert health["source"]["id"] == source_id
    assert health["calls"] == 42
    assert health["total_exec_time_ms"] == pytest.approx(84.0)

    assert client.get("/api/sources/999/trend").status_code == 404
    assert client.get(f"/api/sources/{source_id}/trend?minutes=1").status_code == 422


def test_source_shows_last_collection_and_eviction_warning(
    login: Callable[[str], TestClient],
    sessionmaker: Maker,
) -> None:
    source_id = add_source(sessionmaker)
    collected = datetime.now(UTC) - timedelta(minutes=2)

    async def state() -> None:
        async with sessionmaker() as session:
            session.add(
                CollectorState(
                    source_id=source_id,
                    consecutive_failures=0,
                    last_success_at=collected,
                    last_eviction_at=datetime.now(UTC) - timedelta(minutes=5),
                    evicted_statements=12,
                )
            )
            await session.commit()

    asyncio.run(state())
    source = login("demo").get(f"/api/sources/{source_id}").json()
    assert source["last_collected_at"] is not None
    [warning] = source["warnings"]
    assert "evicted 12 statement(s)" in warning and "pg_stat_statements.max" in warning
