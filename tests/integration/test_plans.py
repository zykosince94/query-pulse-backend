"""Generic plans against real PostgreSQL 16 (QP-AC-15).

A table shaped like the demo's planted problem P1 (orders.customer_id with no index) is
queried, collected and planned. The plan must show the sequential scan, and planning must
never execute the statement: its call count in pg_stat_statements stays exactly the same.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.types.numeric import Int8
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.adapters.base import ConnectionParams
from query_pulse.adapters.postgres import PostgresAdapter
from query_pulse.collector.cycle import Collector
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from query_pulse.workload import plans
from query_pulse.workload.models import Fingerprint, Plan
from query_pulse.workload.plan_tree import parse_plan, walk
from tests.integration.conftest import PgTarget, _collector_target

Maker = async_sessionmaker[AsyncSession]
P1_QUERY = "SELECT id, status, total FROM orders WHERE customer_id = %s"
P1_TEXT = "SELECT id, status, total FROM orders WHERE customer_id = $1"
HIDDEN_QUERY = "SELECT secret FROM vault WHERE id = %s"


@pytest.fixture(scope="module")
def pg_plans() -> Iterator[PgTarget]:
    for target in _collector_target():
        with psycopg.connect(target.superuser_dsn(), autocommit=True) as conn:
            for statement in (
                "CREATE TABLE orders (id bigserial PRIMARY KEY, customer_id bigint NOT NULL,"
                " status text NOT NULL, total numeric(12, 2) NOT NULL)",
                "INSERT INTO orders (customer_id, status, total) SELECT 1 + g % 2000, 'paid',"
                " g % 500 FROM generate_series(1, 50000) g",
                "ANALYZE orders",
                "GRANT SELECT ON orders TO monitor",
                # The monitoring role is NOT granted SELECT here: planning must fail politely.
                "CREATE TABLE vault (id bigint PRIMARY KEY, secret text)",
            ):
                conn.execute(statement)  # fixed test SQL
        yield target


@pytest.fixture
def maker(database_url: str) -> Iterator[Maker]:
    engine = create_engine(database_url)
    yield create_sessionmaker(engine)
    asyncio.run(engine.dispose())


@pytest.fixture
def plan_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "source_connect_timeout_seconds": 2.0,
            "collector_cycle_timeout_seconds": 10.0,
            "collector_plan_seconds": 0.0,  # full re-capture every cycle
        }
    )


def add_source(maker: Maker, settings: Settings, target: PgTarget) -> int:
    async def _add() -> int:
        async with maker() as session:
            source = DataSource(
                name="plans",
                engine=SourceEngine.POSTGRES,
                host=target.host,
                port=target.port,
                database="collect_db",
                username="monitor",
                password_encrypted=CredentialCipher(settings.fernet_keys).encrypt("monitor-pass"),
                tls_mode="disable",
                enabled=True,
                description="",
                status=SourceStatus.HEALTHY,
            )
            session.add(source)
            await session.commit()
            return source.id

    return asyncio.run(_add())


def run_queries(target: PgTarget, query: str, times: int) -> None:
    with psycopg.connect(target.superuser_dsn(), autocommit=True) as conn:
        for i in range(times):
            conn.execute(query, (Int8(1 + i % 2000),))


def direct_calls(target: PgTarget, text: str) -> int:
    with psycopg.connect(target.superuser_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT coalesce(sum(calls), 0) FROM pg_stat_statements WHERE query = %s", (text,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def latest_plan_for(maker: Maker, source_id: int, text: str) -> Plan | None:
    async def _load() -> Plan | None:
        async with maker() as session:
            fingerprint = await session.scalar(
                select(Fingerprint).where(
                    Fingerprint.source_id == source_id, Fingerprint.query == text
                )
            )
            assert fingerprint is not None, f"no fingerprint for {text!r}"
            return await plans.latest_plan(session, fingerprint.id)

    return asyncio.run(_load())


def test_p1_plan_shows_the_seq_scan_without_executing_it(
    pg_plans: PgTarget, maker: Maker, plan_settings: Settings
) -> None:
    source_id = add_source(maker, plan_settings, pg_plans)
    collector = Collector(plan_settings, maker, CredentialCipher(plan_settings.fernet_keys))

    run_queries(pg_plans, P1_QUERY, 20)
    assert asyncio.run(collector.collect(source_id)).ok  # baseline
    run_queries(pg_plans, P1_QUERY, 20)
    assert asyncio.run(collector.collect(source_id)).ok  # deltas, then plans

    plan = latest_plan_for(maker, source_id, P1_TEXT)
    assert plan is not None and plan.error is None, plan and plan.error
    scans = [n for n in walk(parse_plan(plan.plan)) if "Seq Scan" in n.node_type]
    assert scans and scans[0].relation_name == "orders"
    assert scans[0].filter and "customer_id" in scans[0].filter

    # QP-AC-15: planning never executes the statement. Plan it many more times (directly and
    # through further collection cycles) and the call count must not move at all.
    before = direct_calls(pg_plans, P1_TEXT)
    asyncio.run(_explain_repeatedly(pg_plans, P1_TEXT, times=10))
    for _ in range(2):
        assert asyncio.run(collector.collect(source_id)).ok
    assert direct_calls(pg_plans, P1_TEXT) == before

    # The plan was unchanged, so it was re-checked rather than stored again.
    again = latest_plan_for(maker, source_id, P1_TEXT)
    assert again is not None and again.id == plan.id and again.checked_at > plan.checked_at


async def _explain_repeatedly(target: PgTarget, text: str, times: int) -> None:
    adapter = PostgresAdapter()
    params = ConnectionParams(
        host=target.host,
        port=target.port,
        database="collect_db",
        username="monitor",
        password="monitor-pass",
        tls_mode="disable",
        connect_timeout=2.0,
    )
    async with await adapter.connect(params) as conn:
        for _ in range(times):
            document: Any = await adapter.explain_generic(conn, text, "monitor")
            assert document[0]["Plan"]["Node Type"]


def test_unreadable_table_records_why_there_is_no_plan(
    pg_plans: PgTarget, maker: Maker, plan_settings: Settings
) -> None:
    source_id = add_source(maker, plan_settings, pg_plans)
    collector = Collector(plan_settings, maker, CredentialCipher(plan_settings.fernet_keys))
    with psycopg.connect(pg_plans.superuser_dsn(), autocommit=True) as conn:
        conn.execute("INSERT INTO vault VALUES (1, 's') ON CONFLICT DO NOTHING")

    run_queries(pg_plans, HIDDEN_QUERY, 5)
    assert asyncio.run(collector.collect(source_id)).ok
    run_queries(pg_plans, HIDDEN_QUERY, 5)
    assert asyncio.run(collector.collect(source_id)).ok, "a plan failure never fails collection"

    plan = latest_plan_for(maker, source_id, "SELECT secret FROM vault WHERE id = $1")
    assert plan is not None and plan.plan is None
    assert plan.error is not None and "GRANT SELECT" in plan.error and "monitor" in plan.error
