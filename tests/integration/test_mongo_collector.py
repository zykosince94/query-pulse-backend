"""The collector against a real MongoDB 7: profiler collection, shapes, plans, M1 end to end.

- QP-AC-24 (backend half): with profiling off the source reports the capability gap and
  its fix, and nothing is collected; turning profiling on is noticed on the next cycle.
- QP-AC-22 on real data: thirty lookups with thirty different values are one shape.
- ADR 0010 (no stored values): a marker value used in the profiled queries appears nowhere
  in the app database or the logs, not in shapes, plans, observations or findings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest
from pymongo import MongoClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.collector.cycle import Collector
from query_pulse.collector.models import CollectorState
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.findings.models import Finding
from query_pulse.log import configure_logging
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from query_pulse.workload.models import Fingerprint, Observation, Plan, ServerSample
from tests.integration.conftest import MongoServer, _host

Maker = async_sessionmaker[AsyncSession]
MARKER = "marker-value-that-must-never-be-stored"


@pytest.fixture(scope="module")
def mongo_target() -> Iterator[MongoServer]:
    """Its own MongoDB 7 (the profiler level is changed here), with 5,000 orders and a
    monitoring user that has clusterMonitor and read on `shop`."""
    from testcontainers.community.mongodb import MongoDbContainer

    with MongoDbContainer("mongo:7") as container:
        host = _host(container)
        port = int(container.get_exposed_port(27017))
        root_url = (
            f"mongodb://{container.username}:{container.password}@{host}:{port}/"
            "?authSource=admin&directConnection=true"
        )
        client: MongoClient[dict[str, Any]] = MongoClient(root_url)
        with client:
            client.admin.command(
                "createUser",
                "monitor",
                pwd="monitor-pass",
                roles=[{"role": "clusterMonitor", "db": "admin"}, {"role": "read", "db": "shop"}],
            )
            client.shop.orders.insert_many(
                [
                    {"orderNo": i, "customerId": i % 2_500, "email": f"c{i}@x.io"}
                    for i in range(5_000)
                ]
            )
            client.shop.orders.create_index("orderNo")
        yield MongoServer(host, port, root_url, container.username, container.password)


@pytest.fixture
def maker(database_url: str) -> Iterator[Maker]:
    engine = create_engine(database_url)
    yield create_sessionmaker(engine)
    asyncio.run(engine.dispose())


@pytest.fixture
def fast(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "source_connect_timeout_seconds": 3.0,
            "collector_cycle_timeout_seconds": 15.0,
            "collector_capability_check_seconds": 3600.0,
        }
    )


def root(target: MongoServer) -> MongoClient[dict[str, Any]]:
    return MongoClient(target.root_url)


def set_profiling(target: MongoServer, level: int) -> None:
    with root(target) as client:
        client.shop.command("profile", level, slowms=0)


def add_source(maker: Maker, settings: Settings, target: MongoServer) -> int:
    cipher = CredentialCipher(settings.fernet_keys)

    async def _add() -> int:
        async with maker() as session:
            source = DataSource(
                name="mongo",
                engine=SourceEngine.MONGODB,
                host=target.host,
                port=target.port,
                database="shop",
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


def rows[T](maker: Maker, model: type[T]) -> list[T]:
    async def _load() -> list[T]:
        async with maker() as session:
            return list(await session.scalars(select(model)))

    return asyncio.run(_load())


def source_of(maker: Maker, source_id: int) -> DataSource:
    async def _load() -> DataSource:
        async with maker() as session:
            source = await session.get(DataSource, source_id)
            assert source is not None
            return source

    return asyncio.run(_load())


def test_profiling_off_reports_the_gap_and_collects_nothing(
    mongo_target: MongoServer, maker: Maker, fast: Settings
) -> None:
    set_profiling(mongo_target, 0)
    source_id = add_source(maker, fast, mongo_target)
    collector = Collector(fast, maker, CredentialCipher(fast.fernet_keys))
    for _ in range(2):
        assert asyncio.run(collector.collect(source_id)).ok

    source = source_of(maker, source_id)
    profiler = next(c for c in source.capabilities if c.feature == "profiler")
    assert source.status is SourceStatus.DEGRADED
    assert not profiler.available and profiler.fix and "setProfilingLevel(1" in profiler.fix
    assert rows(maker, Observation) == []
    assert len(rows(maker, ServerSample)) == 2  # serverStatus works without the profiler


def test_profiled_queries_become_one_shape_with_a_redacted_plan_and_a_finding(
    mongo_target: MongoServer, maker: Maker, fast: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger()
    previous_level, previous_handlers = logger.level, list(logger.handlers)
    configure_logging("json", "DEBUG")  # as in production, even at the most verbose level
    caplog.set_level(logging.DEBUG)
    try:
        _collect_and_check(mongo_target, maker, fast, caplog)
    finally:
        logger.setLevel(previous_level)
        for handler in [h for h in logger.handlers if h not in previous_handlers]:
            logger.removeHandler(handler)


def _collect_and_check(
    mongo_target: MongoServer, maker: Maker, fast: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    set_profiling(mongo_target, 0)
    source_id = add_source(maker, fast, mongo_target)
    collector = Collector(fast, maker, CredentialCipher(fast.fernet_keys))
    assert asyncio.run(collector.collect(source_id)).ok  # profiling off: checklist says so

    set_profiling(mongo_target, 1)
    assert asyncio.run(collector.collect(source_id)).ok  # notices the change; sets the cursor
    profiler = next(c for c in source_of(maker, source_id).capabilities if c.feature == "profiler")
    assert profiler.available

    with root(mongo_target) as client:
        for customer in range(30):  # M1: no index on customerId
            list(client.shop.orders.find({"customerId": customer}))
        list(client.shop.orders.find({"email": MARKER}))
    time.sleep(1.5)  # the collector reads up to a second before the server's clock
    outcome = asyncio.run(collector.collect(source_id))
    assert outcome.ok and outcome.observations >= 2

    fingerprints = {f.query: f for f in rows(maker, Fingerprint)}
    by_customer = fingerprints['{"find": "orders", "filter": {"customerId": "?"}}']
    assert by_customer.engine == "mongodb" and by_customer.namespace == "shop.orders"
    [observation] = [o for o in rows(maker, Observation) if o.fingerprint_id == by_customer.id]
    assert observation.calls == 30
    assert observation.docs_examined == 30 * 5_000 and observation.keys_examined == 0
    # Query Pulse's own reads (system.profile, explain, checks) are never fingerprinted.
    assert not any("system.profile" in q or "explain" in q for q in fingerprints)

    plans = {p.fingerprint_id: p for p in rows(maker, Plan)}
    plan = plans[by_customer.id]
    assert plan.engine == "mongodb" and plan.error is None and plan.plan is not None
    assert plan.plan[0]["winningPlan"]["stage"] == "COLLSCAN"
    assert plan.plan[0]["winningPlan"]["filter"] == {"customerId": {"$eq": "?"}}

    assert asyncio.run(collector.evaluate_rules(source_id, force=True))
    found = {f.rule_id: f for f in rows(maker, Finding)}
    assert found["MDB-COLLSCAN"].fingerprint_id == by_customer.id
    assert "customerId" in (found["MDB-COLLSCAN"].action_sql or "")

    # No stored values (ADR 0010): the marker is nowhere in what Query Pulse kept or logged.
    stored = json.dumps(
        [
            [f.query for f in rows(maker, Fingerprint)],
            [p.plan for p in rows(maker, Plan)],
            [p.error for p in rows(maker, Plan)],
            [(f.summary, f.facts, f.evidence, f.action_sql) for f in rows(maker, Finding)],
            [s.last_error for s in rows(maker, CollectorState)],
        ],
        default=str,
    )
    assert MARKER not in stored
    assert MARKER not in caplog.text
