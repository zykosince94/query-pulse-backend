"""Findings across real collection cycles against PostgreSQL 16 (QP-AC-21, QP-AC-18).

A P1-shaped table (no index on the looked-up column) is queried and collected; the collector
captures the plan and runs the rules, producing one PG-SEQSCAN-HOT finding. The admin dismisses
it through the API, and it must stay dismissed, as one row, through later cycles in which the
rule keeps firing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.numeric import Int8
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.collector.cycle import Collector
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.findings.models import Finding
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from tests.integration.conftest import PgTarget, _collector_target

Maker = async_sessionmaker[AsyncSession]
HOT_QUERY = "SELECT id, total FROM accounts WHERE owner_id = %s"


@pytest.fixture(scope="module")
def pg_findings() -> Iterator[PgTarget]:
    for target in _collector_target():
        with psycopg.connect(target.superuser_dsn(), autocommit=True) as conn:
            for statement in (
                "CREATE TABLE accounts (id bigserial PRIMARY KEY, owner_id bigint NOT NULL,"
                " total numeric(12, 2) NOT NULL)",
                "INSERT INTO accounts (owner_id, total) SELECT 1 + g % 5000, g % 900"
                " FROM generate_series(1, 60000) g",
                "ANALYZE accounts",
                "GRANT SELECT ON accounts TO monitor",
            ):
                conn.execute(statement)  # fixed test SQL
        yield target


@pytest.fixture
def maker(database_url: str) -> Iterator[Maker]:
    engine = create_engine(database_url)
    yield create_sessionmaker(engine)
    asyncio.run(engine.dispose())


@pytest.fixture
def cycle_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "source_connect_timeout_seconds": 2.0,
            "collector_cycle_timeout_seconds": 10.0,
            "collector_relation_stats_seconds": 0.0,  # sample tables every cycle
            "collector_plan_seconds": 0.0,
            "rules_interval_seconds": 0.0,  # rules after every cycle
        }
    )


def add_source(maker: Maker, settings: Settings, target: PgTarget) -> int:
    async def _add() -> int:
        async with maker() as session:
            source = DataSource(
                name="findings",
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


def run_hot_query(target: PgTarget, times: int) -> None:
    with psycopg.connect(target.superuser_dsn(), autocommit=True) as conn:
        for i in range(times):
            conn.execute(HOT_QUERY, (Int8(1 + i % 5000),))


def seqscan_findings(maker: Maker, source_id: int) -> list[Finding]:
    async def _load() -> list[Finding]:
        async with maker() as session:
            rows = await session.scalars(
                select(Finding).where(
                    Finding.source_id == source_id, Finding.rule_id == "PG-SEQSCAN-HOT"
                )
            )
            return [f for f in rows if f.relation == "public.accounts"]

    return asyncio.run(_load())


def test_dismissed_finding_stays_dismissed_across_cycles(
    pg_findings: PgTarget,
    maker: Maker,
    cycle_settings: Settings,
    login: Callable[[str], TestClient],
) -> None:
    source_id = add_source(maker, cycle_settings, pg_findings)
    collector = Collector(cycle_settings, maker, CredentialCipher(cycle_settings.fernet_keys))

    run_hot_query(pg_findings, 30)
    assert asyncio.run(collector.collect(source_id)).ok  # baseline
    run_hot_query(pg_findings, 30)
    assert asyncio.run(collector.collect(source_id)).ok  # deltas, plan, rules

    [finding] = seqscan_findings(maker, source_id)
    assert finding.status == "open"
    assert finding.action_sql == "CREATE INDEX CONCURRENTLY ON public.accounts (owner_id);"

    client = login("admin")
    body = client.get(f"/api/findings/{finding.id}").json()
    assert all(item["available"] for item in body["evidence"]), body["evidence"]  # QP-AC-20
    response = client.patch(f"/api/findings/{finding.id}", json={"status": "dismissed"})
    assert response.status_code == 200

    for _ in range(3):
        run_hot_query(pg_findings, 30)
        assert asyncio.run(collector.collect(source_id)).ok

    [after] = seqscan_findings(maker, source_id)  # still exactly one row
    assert after.id == finding.id
    assert after.status == "dismissed", "the rule kept firing, the dismissal held"
    assert after.last_seen_at > finding.last_seen_at
