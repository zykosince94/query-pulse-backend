"""The collector against real PostgreSQL: QP-AC-09 to QP-AC-13.

Each test gets its own app database (SQLite, migrated) and a Collector pointed at a
PostgreSQL container, then runs collection cycles by hand around known activity.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.types.numeric import Int8
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.collector.cycle import Collector, CycleOutcome
from query_pulse.collector.models import CollectorState
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from query_pulse.workload.models import Fingerprint, Observation, RelationStat
from tests.integration.conftest import PgTarget

Maker = async_sessionmaker[AsyncSession]
KNOWN_QUERY = "SELECT %s::bigint * 2 AS doubled"
KNOWN_TEXT = "SELECT $1::bigint * $2 AS doubled"


@pytest.fixture
def maker(database_url: str) -> Iterator[Maker]:
    engine = create_engine(database_url)
    yield create_sessionmaker(engine)
    asyncio.run(engine.dispose())


@pytest.fixture
def fast_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "source_connect_timeout_seconds": 2.0,
            "collector_cycle_timeout_seconds": 5.0,
            "collector_relation_stats_seconds": 0.0,  # every cycle
            "collector_capability_check_seconds": 3600.0,
        }
    )


def add_source(maker: Maker, settings: Settings, target: PgTarget) -> int:
    cipher = CredentialCipher(settings.fernet_keys)

    async def _add() -> int:
        async with maker() as session:
            source = DataSource(
                name=f"pg-{target.port}",
                engine=SourceEngine.POSTGRES,
                host=target.host,
                port=target.port,
                database="collect_db",
                username="monitor",
                password_encrypted=cipher.encrypt("monitor-pass"),
                tls_mode="disable",
                enabled=True,
                description="",
                status=SourceStatus.HEALTHY,
            )
            session.add(source)
            await session.commit()
            return source.id

    return asyncio.run(_add())


def collector(maker: Maker, settings: Settings) -> Collector:
    return Collector(settings, maker, CredentialCipher(settings.fernet_keys))


def cycle(c: Collector, source_id: int) -> CycleOutcome:
    return asyncio.run(c.collect(source_id))


def run_known_query(target: PgTarget, times: int) -> None:
    with psycopg.connect(target.superuser_dsn(), autocommit=True) as conn:
        for i in range(times):
            conn.execute(KNOWN_QUERY, (Int8(i),))


def direct_counters(target: PgTarget) -> tuple[int, float]:
    """(calls, total_exec_time) for the known query, straight from pg_stat_statements."""
    with psycopg.connect(target.superuser_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT coalesce(sum(calls), 0), coalesce(sum(total_exec_time), 0) "
            "FROM pg_stat_statements WHERE query = %s",
            (KNOWN_TEXT,),
        ).fetchone()
    assert row is not None
    return int(row[0]), float(row[1])


def known_observations(maker: Maker, source_id: int) -> list[Observation]:
    async def _load() -> list[Observation]:
        async with maker() as session:
            rows = await session.scalars(
                select(Observation)
                .join(Fingerprint, Fingerprint.id == Observation.fingerprint_id)
                .where(Fingerprint.source_id == source_id, Fingerprint.query == KNOWN_TEXT)
                .order_by(Observation.interval_start)
            )
            return list(rows)

    return asyncio.run(_load())


def load(maker: Maker, model: type[Any], key: int) -> Any:
    async def _get() -> Any:
        async with maker() as session:
            return await session.get(model, key)

    return asyncio.run(_get())


# --- QP-AC-09: exact counts, and a mean that matches pg_stat_statements --------------------------


def test_500_calls_between_cycles_cross_checked(
    maker: Maker, fast_settings: Settings, pg_collect: PgTarget
) -> None:
    source_id = add_source(maker, fast_settings, pg_collect)
    c = collector(maker, fast_settings)
    run_known_query(pg_collect, 1)  # make sure the statement exists before the baseline

    assert cycle(c, source_id).discarded == "baseline"
    before = direct_counters(pg_collect)
    run_known_query(pg_collect, 500)
    after = direct_counters(pg_collect)
    outcome = cycle(c, source_id)
    assert outcome.ok and outcome.discarded is None

    [observation] = known_observations(maker, source_id)
    assert observation.calls == 500
    direct_mean = (after[1] - before[1]) / (after[0] - before[0])
    assert observation.mean_exec_time_ms == pytest.approx(direct_mean, rel=0.01)


def test_collector_excludes_its_own_queries_and_samples_relations(
    maker: Maker, fast_settings: Settings, pg_collect: PgTarget
) -> None:
    source_id = add_source(maker, fast_settings, pg_collect)
    c = collector(maker, fast_settings)
    cycle(c, source_id)
    run_known_query(pg_collect, 5)
    cycle(c, source_id)

    async def check() -> tuple[list[str], int]:
        async with maker() as session:
            texts = list(
                await session.scalars(
                    select(Fingerprint.query).where(Fingerprint.source_id == source_id)
                )
            )
            widgets = await session.scalar(
                select(func.count())
                .select_from(RelationStat)
                .where(RelationStat.source_id == source_id, RelationStat.table_name == "widgets")
            )
        return texts, int(widgets or 0)

    texts, widget_samples = asyncio.run(check())
    assert texts, "the workload's statements were recorded"
    assert not any("query-pulse" in t for t in texts), "our own statements are excluded"
    assert widget_samples >= 2, "table and primary-key index sampled"


# --- QP-AC-10: a reset never produces negative or inflated numbers -----------------------------


def test_reset_discards_the_interval_and_collection_continues(
    maker: Maker, fast_settings: Settings, pg_collect: PgTarget
) -> None:
    source_id = add_source(maker, fast_settings, pg_collect)
    c = collector(maker, fast_settings)
    run_known_query(pg_collect, 1)
    cycle(c, source_id)  # baseline

    run_known_query(pg_collect, 50)
    assert cycle(c, source_id).discarded is None

    with psycopg.connect(pg_collect.superuser_dsn(), autocommit=True) as conn:
        conn.execute("SELECT pg_stat_statements_reset()")
    run_known_query(pg_collect, 30)
    reset_cycle = cycle(c, source_id)
    assert reset_cycle.ok
    assert reset_cycle.discarded is not None and "reset" in reset_cycle.discarded

    run_known_query(pg_collect, 20)
    assert cycle(c, source_id).discarded is None

    calls = [o.calls for o in known_observations(maker, source_id)]
    assert calls == [50, 20], "the reset interval was dropped; nothing negative or inflated"
    assert all(o.total_exec_time_ms >= 0 for o in known_observations(maker, source_id))


# --- QP-AC-11: a restarted collector keeps its baseline ------------------------------------------


def test_restarted_collector_continues_from_the_persisted_baseline(
    maker: Maker, fast_settings: Settings, pg_collect: PgTarget
) -> None:
    source_id = add_source(maker, fast_settings, pg_collect)
    run_known_query(pg_collect, 1)
    cycle(collector(maker, fast_settings), source_id)  # first process: baseline

    run_known_query(pg_collect, 100)
    restarted = collector(maker, fast_settings)  # a brand-new process, same app database
    outcome = restarted.collect(source_id)
    assert asyncio.run(outcome).discarded is None, "no new baseline needed after a restart"

    [observation] = known_observations(maker, source_id)
    assert observation.calls == 100


# --- QP-AC-12: evictions produce a warning on the source ----------------------------------------


def test_rising_dealloc_warns_on_the_source(
    maker: Maker, fast_settings: Settings, pg_small_max: PgTarget
) -> None:
    source_id = add_source(maker, fast_settings, pg_small_max)
    c = collector(maker, fast_settings)
    cycle(c, source_id)  # baseline

    # 150 distinct statement shapes (different column counts) overflow a 100-entry table.
    with psycopg.connect(pg_small_max.superuser_dsn(), autocommit=True) as conn:
        for width in range(1, 151):
            conn.execute("SELECT " + ", ".join(["1"] * width))
    assert cycle(c, source_id).ok

    state = load(maker, CollectorState, source_id)
    assert state.evicted_statements > 0
    source = load(maker, DataSource, source_id)
    assert source.status is SourceStatus.DEGRADED


# --- QP-AC-13: a database going down marks the source failing; it recovers on its own -----------


def test_outage_marks_failing_then_recovers(
    maker: Maker, fast_settings: Settings, pg_pausable: PgTarget
) -> None:
    source_id = add_source(maker, fast_settings, pg_pausable)
    c = collector(maker, fast_settings)
    assert cycle(c, source_id).ok
    assert load(maker, DataSource, source_id).status is SourceStatus.HEALTHY

    docker_container = pg_pausable.container.get_wrapped_container()
    docker_container.pause()
    try:
        first = cycle(c, source_id)
        second = cycle(c, source_id)
    finally:
        docker_container.unpause()
    assert not first.ok and not second.ok
    assert second.consecutive_failures == 2, "failures counted, so the supervisor backs off"
    down = load(maker, DataSource, source_id)
    assert down.status is SourceStatus.FAILING
    assert down.last_error

    recovered = cycle(c, source_id)
    assert recovered.ok
    up = load(maker, DataSource, source_id)
    assert up.status is SourceStatus.HEALTHY
    assert up.last_error is None
    assert load(maker, CollectorState, source_id).consecutive_failures == 0
