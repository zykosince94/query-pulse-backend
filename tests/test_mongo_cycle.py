"""The MongoDB collection cycle and its API, with a scripted MongoDB (SQLite app database).

The real server is in tests/integration/test_mongo_collector.py; here a fake adapter makes
the edge cases easy to reach: the first cycle's baseline, reading each entry once, a full
system.profile, a read that hits the per-cycle limit, explain errors, the profiler being
switched off. Plus the API: server metrics, MongoDB metrics in the explorer, plans.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.adapters.base import Capability, ProbeResult
from query_pulse.adapters.mongodb import MongoAdapter, ProfileEntry, ProfileReading, ServerStatus
from query_pulse.collector.cycle import Collector
from query_pulse.collector.models import CollectorState
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from query_pulse.workload.models import Fingerprint, Observation, Plan, ServerSample
from query_pulse.workload.service import as_utc

PLANS = Path(__file__).parent / "fixtures" / "mongo_plans"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
Maker = async_sessionmaker[AsyncSession]


class FakeMongo:
    """Stands in for MongoAdapter: a scripted profiler and explain, no server."""

    def __init__(self) -> None:
        self.clock = NOW
        self.entries: list[ProfileEntry] = []
        self.overflowed = False
        self.level = 1
        self.explained: list[dict[str, Any]] = []
        self.explain_error: Exception | None = None
        self.reads: list[tuple[datetime, datetime]] = []

    async def probe(self, params: Any) -> ProbeResult:
        on = self.level > 0
        return ProbeResult(
            "7.0.0",
            1.0,
            [
                Capability("server_status", "Server metrics (serverStatus)", True),
                Capability("explain", "Query plans (explain)", True),
                Capability(
                    "profiler",
                    "Slow operations (profiler)",
                    on,
                    None if on else "Profiling is off on database 'shop'.",
                    None if on else "db.setProfilingLevel(1, { slowms: 100 })",
                ),
            ],
        )

    async def connect(self, params: Any) -> FakeMongo:
        return self

    async def close(self) -> None:
        return None

    async def server_time(self, client: Any) -> datetime:
        return self.clock

    async def server_status(self, client: Any) -> ServerStatus:
        return ServerStatus(100.0, {"query": 10}, 3, 100, 1, 2, "primary")

    async def profiling_level(self, client: Any, database: str) -> int:
        return self.level

    async def read_profile(
        self, client: Any, database: str, after: datetime, until: datetime, limit: int
    ) -> ProfileReading:
        self.reads.append((after, until))
        found = [e for e in self.entries if after < e.ts <= until]
        return ProfileReading(found[:limit], self.overflowed, truncated=len(found) > limit)

    async def explain(self, client: Any, database: str, command: dict[str, Any]) -> dict[str, Any]:
        self.explained.append(command)
        if self.explain_error is not None:
            raise self.explain_error
        data: dict[str, Any] = json.loads(
            (PLANS / "collscan_customer.json").read_text(encoding="utf-8")
        )
        return data


@pytest.fixture
def maker(database_url: str) -> Iterator[Maker]:
    db = create_engine(database_url)
    yield create_sessionmaker(db)
    asyncio.run(db.dispose())


def add_mongo_source(maker: Maker) -> int:
    async def _add() -> int:
        async with maker() as session:
            source = DataSource(
                name="Mongo",
                engine=SourceEngine.MONGODB,
                host="mongo.example",
                port=27017,
                database="shop",
                username="",
                tls_mode="disable",
                enabled=True,
                description="",
                status=SourceStatus.HEALTHY,
            )
            session.add(source)
            await session.commit()
            return source.id

    return asyncio.run(_add())


def load[T](maker: Maker, model: type[T]) -> list[T]:
    async def _load() -> list[T]:
        async with maker() as session:
            return list(await session.scalars(select(model)))

    return asyncio.run(_load())


def lookup(customer: int, seconds: float) -> ProfileEntry:
    return ProfileEntry(
        op="query",
        ns="shop.orders",
        millis=4.0,
        nreturned=2,
        docs_examined=50_000,
        keys_examined=0,
        ts=NOW + timedelta(seconds=seconds),
        command={"find": "orders", "filter": {"customerId": customer}, "lsid": {"id": 1}},
    )


def scripted(maker: Maker, settings: Settings, fake: FakeMongo, **overrides: Any) -> Collector:
    fast = settings.model_copy(update={"collector_capability_check_seconds": 3600.0, **overrides})
    return Collector(
        fast, maker, CredentialCipher(fast.fernet_keys), mongo_adapter=cast(MongoAdapter, fake)
    )


def cycle(collector: Collector, fake: FakeMongo, source_id: int, at_seconds: float) -> None:
    fake.clock = NOW + timedelta(seconds=at_seconds)
    assert asyncio.run(collector.collect(source_id)).ok


def test_first_cycle_sets_the_cursor_then_each_entry_is_counted_once(
    maker: Maker, settings: Settings
) -> None:
    fake = FakeMongo()
    source_id = add_mongo_source(maker)
    collector = scripted(maker, settings, fake)
    fake.entries = [lookup(n, -30 + n) for n in range(5)]  # before the first cycle

    cycle(collector, fake, source_id, 0)
    assert load(maker, Observation) == []  # baseline only: a backlog has no interval
    assert fake.reads == []
    [state] = load(maker, CollectorState)
    assert state.profile_cursor is not None
    assert as_utc(state.profile_cursor) == NOW - timedelta(seconds=1)  # settles a second

    fake.entries += [lookup(n, 2 + n) for n in range(12)]
    cycle(collector, fake, source_id, 30)
    [observation] = load(maker, Observation)
    assert (observation.calls, observation.docs_examined, observation.rows) == (12, 600_000, 24)
    assert observation.keys_examined == 0 and observation.total_exec_time_ms == 48.0

    # The next cycle reads after the previous cursor: nothing is counted twice.
    cycle(collector, fake, source_id, 60)
    assert len(load(maker, Observation)) == 1
    assert fake.reads[-1][0] == NOW + timedelta(seconds=29)


def test_read_shapes_are_explained_from_memory_and_only_redacted_plans_are_stored(
    maker: Maker, settings: Settings
) -> None:
    fake = FakeMongo()
    source_id = add_mongo_source(maker)
    collector = scripted(maker, settings, fake)
    cycle(collector, fake, source_id, 0)
    fake.entries = [lookup(424242, 2)]
    cycle(collector, fake, source_id, 30)

    # Explained with the real command (held in memory), minus session metadata...
    assert fake.explained == [{"find": "orders", "filter": {"customerId": 424242}}]
    [plan] = load(maker, Plan)
    assert plan.engine == "mongodb" and plan.error is None
    # ...but nothing stored has the value: not the shape, not the plan (ADR 0010).
    stored = json.dumps([plan.plan, [f.query for f in load(maker, Fingerprint)]])
    assert "424242" not in stored and '"?"' in stored
    assert len(collector.samples[source_id]) == 1


def test_explain_errors_are_stored_without_mongodbs_own_message(
    maker: Maker, settings: Settings
) -> None:
    fake = FakeMongo()
    # MongoDB's message can quote the query's values; only the code and its name are kept.
    fake.explain_error = OperationFailure(
        "error processing query: customerId == 424242", code=2, details={"codeName": "BadValue"}
    )
    source_id = add_mongo_source(maker)
    collector = scripted(maker, settings, fake)
    cycle(collector, fake, source_id, 0)
    fake.entries = [lookup(424242, 2)]
    cycle(collector, fake, source_id, 30)
    [plan] = load(maker, Plan)
    assert plan.plan is None and plan.error == "explain failed (MongoDB error 2 BadValue)."


def test_a_full_system_profile_degrades_the_source_with_a_warning(
    maker: Maker, settings: Settings, login: Callable[[str], TestClient]
) -> None:
    fake = FakeMongo()
    source_id = add_mongo_source(maker)
    collector = scripted(maker, settings, fake)
    cycle(collector, fake, source_id, 0)
    fake.overflowed = True
    cycle(collector, fake, source_id, 30)
    body = login("demo").get(f"/api/sources/{source_id}").json()
    assert body["status"] == "degraded"
    assert any("system.profile" in w for w in body["warnings"])


def test_turning_the_profiler_off_is_noticed_and_listed_as_a_gap(
    maker: Maker, settings: Settings, login: Callable[[str], TestClient]
) -> None:
    """QP-AC-24 (API): the source list carries the gap and its fix, for the explorer."""
    fake = FakeMongo()
    source_id = add_mongo_source(maker)
    collector = scripted(maker, settings, fake)
    cycle(collector, fake, source_id, 0)
    fake.level = 0
    cycle(collector, fake, source_id, 30)
    [listed] = login("demo").get("/api/sources").json()
    assert listed["id"] == source_id and listed["status"] == "degraded"
    [gap] = listed["capability_gaps"]
    assert gap["feature"] == "profiler" and "setProfilingLevel" in gap["fix"]


def test_a_read_at_the_per_cycle_limit_continues_after_its_last_entry(
    maker: Maker, settings: Settings
) -> None:
    fake = FakeMongo()
    source_id = add_mongo_source(maker)
    collector = scripted(maker, settings, fake, collector_profile_max_entries=100)
    cycle(collector, fake, source_id, 0)
    fake.entries = [lookup(n, 1 + n * 0.1) for n in range(150)]
    cycle(collector, fake, source_id, 30)
    [state] = load(maker, CollectorState)
    assert state.profile_cursor is not None
    assert as_utc(state.profile_cursor) == fake.entries[99].ts
    cycle(collector, fake, source_id, 60)
    assert sum(o.calls for o in load(maker, Observation)) == 150


# --- API -------------------------------------------------------------------------------------


def test_server_metrics_give_rates_and_handle_a_restart(
    maker: Maker, login: Callable[[str], TestClient]
) -> None:
    source_id = add_mongo_source(maker)
    client = login("demo")
    assert client.get(f"/api/sources/{source_id}/server").json() is None

    async def sample(at: datetime, uptime: float, queries: int) -> None:
        async with maker() as session:
            session.add(
                ServerSample(
                    source_id=source_id,
                    captured_at=at,
                    uptime_seconds=uptime,
                    opcounters={"query": queries, "insert": 5},
                    connections_current=4,
                    connections_available=96,
                    cache_bytes=10,
                    cache_max_bytes=100,
                    repl_state="primary",
                )
            )
            await session.commit()

    asyncio.run(sample(NOW, 1_000, 100))
    first = client.get(f"/api/sources/{source_id}/server").json()
    assert first["rates_per_second"] is None and first["opcounters"]["query"] == 100
    asyncio.run(sample(NOW + timedelta(seconds=20), 1_020, 300))
    second = client.get(f"/api/sources/{source_id}/server").json()
    assert second["rates_per_second"] == {"query": 10.0, "insert": 0.0}
    assert second["rate_seconds"] == 20
    asyncio.run(sample(NOW + timedelta(seconds=40), 5, 3))  # restarted: counters began again
    assert client.get(f"/api/sources/{source_id}/server").json()["rates_per_second"] is None
    assert client.get("/api/sources/9999/server").status_code == 404


def test_explorer_and_plans_speak_mongodb(
    maker: Maker, settings: Settings, login: Callable[[str], TestClient]
) -> None:
    fake = FakeMongo()
    source_id = add_mongo_source(maker)
    collector = scripted(maker, settings, fake)
    cycle(collector, fake, source_id, 0)
    fake.entries = [lookup(n, 1 + n) for n in range(3)]
    cycle(collector, fake, source_id, 30)

    client = login("demo")
    # Observations are stamped with the collector's clock (the fake only moves the server's).
    [op] = client.get("/api/operations", params={"minutes": 60}).json()["operations"]
    assert (op["engine"], op["namespace"], op["command"]) == ("mongodb", "shop.orders", "find")
    assert (op["calls"], op["docs_examined"], op["keys_examined"]) == (3, 150_000, 0)
    assert op["query"] == '{"find": "orders", "filter": {"customerId": "?"}}'
    plan = client.get(f"/api/operations/{op['id']}/plan").json()
    assert plan["engine"] == "mongodb" and plan["status"] == "captured"
    assert plan["stage_root"]["stage"] == "COLLSCAN" and plan["root"] is None
    assert plan["raw"][0]["winningPlan"]["filter"] == {"customerId": {"$eq": "?"}}

    # No on-demand capture for MongoDB: the admin's POST returns the stored plan unchanged.
    recaptured = login("admin").post(f"/api/operations/{op['id']}/plan").json()
    assert recaptured["plan_id"] == plan["plan_id"] and len(fake.explained) == 1


def test_commands_are_forgotten_when_a_source_points_at_another_server(
    maker: Maker, settings: Settings
) -> None:
    """A command holds its server's values: it's never explained on a different server."""
    fake = FakeMongo()
    source_id = add_mongo_source(maker)
    collector = scripted(maker, settings, fake)
    cycle(collector, fake, source_id, 0)
    fake.entries = [lookup(424242, 2)]
    cycle(collector, fake, source_id, 30)
    assert len(collector.samples[source_id]) == 1

    async def retarget() -> None:
        async with maker() as session:
            source = await session.get(DataSource, source_id)
            assert source is not None
            source.host = "other.example"
            await session.commit()

    asyncio.run(retarget())
    cycle(collector, fake, source_id, 60)
    assert len(collector.samples[source_id]) == 0
