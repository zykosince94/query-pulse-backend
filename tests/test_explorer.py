"""Workload explorer and plans without a monitored database (Phase 4).

Ranking, windows, history buckets and plan states run against the SQLite app database. The
plan parser reads real EXPLAIN (GENERIC_PLAN, FORMAT JSON) output captured from the demo
database (tests/fixtures/plans/). QP-AC-14 and QP-AC-15 themselves are proven against real
PostgreSQL in tests/integration and tests/acceptance.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.adapters.postgres import PlanUnavailableError, plannable_text
from query_pulse.collector.cycle import Collector
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.sources.models import DataSource, SourceCapability, SourceEngine, SourceStatus
from query_pulse.workload import explorer, plans
from query_pulse.workload.explorer import Window
from query_pulse.workload.models import Fingerprint, Observation, Plan
from query_pulse.workload.plan_tree import PlanFormatError, parse_plan, walk

Maker = async_sessionmaker[AsyncSession]
FIXTURES = Path(__file__).parent / "fixtures" / "plans"


def plan_fixture(name: str) -> list[Any]:
    data: list[Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


# --- which statements may be planned ---------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM orders WHERE customer_id = $1",
        "  select 1;",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "/* query-pulse */ SELECT 1",
        "-- a comment\nUPDATE orders SET status = $1 WHERE id = $2",
        "DELETE FROM orders WHERE id = $1",
        "INSERT INTO t VALUES ($1)",
        "VALUES ($1, $2)",
        "TABLE orders",
    ],
)
def test_plannable_statements_pass(query: str) -> None:
    text = plannable_text(query)
    assert not text.endswith(";")


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("SELECT 1; SET default_transaction_read_only = off", "semicolon"),
        ("SELECT 1; DROP TABLE orders;", "semicolon"),
        ("SELECT ';' AS s", "semicolon"),  # refused even inside a literal: better safe
        ("BEGIN", "Only SELECT"),
        ("SET work_mem = $1", "Only SELECT"),
        ("VACUUM orders", "Only SELECT"),
        ("EXPLAIN SELECT 1", "Only SELECT"),
        ("selection", "Only SELECT"),  # a word that starts with "select" isn't SELECT
        ("SELECT " + "x" * 10_000, "cut off"),
    ],
)
def test_unplannable_statements_are_refused_with_a_reason(query: str, reason: str) -> None:
    with pytest.raises(PlanUnavailableError, match=reason):
        plannable_text(query)


# --- plan parsing (real plans from the demo database) ----------------------------------------


def test_p1_plan_parses_to_a_seq_scan_on_orders() -> None:
    root = parse_plan(plan_fixture("p1_orders_by_customer"))
    scans = [n for n in walk(root) if "Seq Scan" in n.node_type]
    assert scans and scans[0].relation_name == "orders"
    assert scans[0].filter is not None and "customer_id" in scans[0].filter
    assert root.path == "0"
    assert root.total_cost >= scans[0].total_cost > 0
    assert all(n.plan_rows >= 0 for n in walk(root))


def test_index_lookup_plan_names_its_index() -> None:
    root = parse_plan(plan_fixture("c1_customer_by_id"))
    [index_scan] = [n for n in walk(root) if n.node_type == "Index Scan"]
    assert index_scan.index_name == "customers_pkey"
    assert index_scan.index_cond is not None


def test_p2_plan_sorts_under_a_limit() -> None:
    root = parse_plan(plan_fixture("p2_latest_orders"))
    assert root.node_type == "Limit"
    nodes = list(walk(root))
    assert any(n.node_type == "Sort" and n.sort_key for n in nodes)
    assert [n.path for n in nodes][:2] == ["0", "0.0"]


def test_unknown_keys_are_kept_as_details() -> None:
    root = parse_plan(
        [{"Plan": {"Node Type": "Result", "Total Cost": 0.01, "Async Capable": False}}]
    )
    assert root.details == {"Async Capable": "False"}


@pytest.mark.parametrize(
    "document",
    [{}, [], [{}], [{"Plan": {}}], [{"Plan": {"Node Type": "X", "Plans": "nope"}}]],
)
def test_malformed_plans_are_rejected(document: Any) -> None:
    with pytest.raises(PlanFormatError):
        parse_plan(document)


# --- fixtures: a source with operations and observations -------------------------------------


@pytest.fixture
def maker(database_url: str) -> Iterator[Maker]:
    engine = create_engine(database_url)
    yield create_sessionmaker(engine)
    asyncio.run(engine.dispose())


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def add_source(
    maker: Maker,
    name: str = "Orders DB",
    engine: SourceEngine = SourceEngine.POSTGRES,
    plans_ok: bool | None = True,
) -> int:
    async def _add() -> int:
        async with maker() as session:
            source = DataSource(
                name=name,
                engine=engine,
                host="db.example",
                port=5432,
                database="shop",
                username="monitor",
                tls_mode="disable",
                enabled=True,
                description="",
                status=SourceStatus.HEALTHY,
            )
            if plans_ok is not None:
                source.capabilities.append(
                    SourceCapability(
                        position=0,
                        feature="generic_plans",
                        label="Query plans (EXPLAIN GENERIC_PLAN)",
                        available=plans_ok,
                        reason=None if plans_ok else "Generic plans need PostgreSQL 16 or later.",
                        fix=None if plans_ok else "Upgrade to PostgreSQL 16 or later.",
                    )
                )
            session.add(source)
            await session.commit()
            return source.id

    return run(_add())


def add_operation(
    maker: Maker,
    source_id: int,
    query: str,
    intervals: list[tuple[datetime, int, float]],
    queryid: int | None = None,
) -> int:
    """A fingerprint plus one observation per (interval_end, calls, total_ms)."""

    async def _add() -> int:
        async with maker() as session:
            fp = Fingerprint(
                source_id=source_id,
                dbid=1,
                userid=1,
                queryid=queryid if queryid is not None else abs(hash(query)) % 10**12,
                toplevel=True,
                query=query,
                first_seen_at=min(i[0] for i in intervals),
                last_seen_at=max(i[0] for i in intervals),
            )
            session.add(fp)
            await session.flush()
            for end, calls, total in intervals:
                session.add(
                    Observation(
                        fingerprint_id=fp.id,
                        source_id=source_id,
                        interval_start=end - timedelta(seconds=15),
                        interval_end=end,
                        calls=calls,
                        total_exec_time_ms=total,
                        mean_exec_time_ms=total / calls if calls else 0.0,
                        rows=calls,
                        shared_blks_hit=10,
                        shared_blks_read=1,
                    )
                )
            await session.commit()
            return fp.id

    return run(_add())


NOW = datetime.now(UTC).replace(second=30, microsecond=0)


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


@pytest.fixture
def workload(maker: Maker) -> dict[str, int]:
    """Three operations whose order differs by total time, mean time and calls."""
    source = add_source(maker)
    ids = {
        "source": source,
        # Most total time (100 + 200 = 300 ms) from few calls: mean 30 ms.
        "slow": add_operation(
            maker,
            source,
            "SELECT * FROM orders WHERE customer_id = $1",
            [(ago(2), 4, 100.0), (ago(1), 6, 200.0)],
        ),
        # Highest mean (50 ms), little total.
        "spiky": add_operation(maker, source, "SELECT * FROM big_report", [(ago(1), 2, 100.0)]),
        # Most calls, cheap.
        "chatty": add_operation(
            maker, source, "SELECT 1 FROM t WHERE id = $1", [(ago(1), 1000, 50.0)]
        ),
        # Outside a 15-minute window.
        "old": add_operation(maker, source, "SELECT old_100% FROM x", [(ago(30), 9, 9000.0)]),
    }
    return ids


# --- ranking ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("total_time", ["slow", "spiky", "chatty"]),
        ("mean_time", ["spiky", "slow", "chatty"]),
        ("calls", ["chatty", "slow", "spiky"]),
    ],
)
def test_ranking_orders_by_the_chosen_metric(
    maker: Maker, workload: dict[str, int], sort: str, expected: list[str]
) -> None:
    async def _rank() -> list[int]:
        async with maker() as session:
            result = await explorer.rank_operations(
                session,
                Window.last(15, now=NOW),
                sort=sort,  # type: ignore[arg-type]
            )
            return [o.id for o in result.operations]

    assert run(_rank()) == [workload[name] for name in expected]


def test_metrics_are_window_sums_and_mean_is_weighted(
    maker: Maker, workload: dict[str, int]
) -> None:
    async def _rank() -> Any:
        async with maker() as session:
            return await explorer.rank_operations(session, Window.last(15, now=NOW))

    result = run(_rank())
    slow = result.operations[0]
    assert (slow.calls, slow.total_exec_time_ms) == (10, 300.0)
    # Weighted: 300 / 10 = 30, not the average of the interval means (25 and 33.3).
    assert slow.mean_exec_time_ms == pytest.approx(30.0)
    assert result.total_count == 3
    assert result.total_exec_time_ms == pytest.approx(450.0)
    assert slow.share_of_total_time == pytest.approx(300 / 450)
    assert (slow.shared_blks_hit, slow.shared_blks_read, slow.rows) == (20, 2, 10)


def test_wider_window_includes_older_operations(maker: Maker, workload: dict[str, int]) -> None:
    async def _ids(minutes: int) -> list[int]:
        async with maker() as session:
            result = await explorer.rank_operations(session, Window.last(minutes, now=NOW))
            return [o.id for o in result.operations]

    assert workload["old"] not in run(_ids(15))
    assert run(_ids(60))[0] == workload["old"]


def test_text_filter_matches_case_insensitively_and_literally(
    maker: Maker, workload: dict[str, int]
) -> None:
    async def _ids(text: str) -> list[int]:
        async with maker() as session:
            result = await explorer.rank_operations(session, Window.last(60, now=NOW), text=text)
            return [o.id for o in result.operations]

    assert run(_ids("CUSTOMER_ID")) == [workload["slow"]]
    # "%" is a literal, not a LIKE wildcard: only the statement containing "100%" matches.
    assert run(_ids("100%")) == [workload["old"]]
    # Likewise "_": as a wildcard it would match every statement, including this one.
    assert workload["chatty"] not in run(_ids("_"))


def test_source_filter_and_limit(maker: Maker, workload: dict[str, int]) -> None:
    other = add_source(maker, name="Other DB")
    add_operation(maker, other, "SELECT * FROM elsewhere", [(ago(1), 1, 99_999.0)])

    async def _rank(**kwargs: Any) -> Any:
        async with maker() as session:
            return await explorer.rank_operations(session, Window.last(15, now=NOW), **kwargs)

    only = run(_rank(source_id=workload["source"]))
    assert {o.source_id for o in only.operations} == {workload["source"]}
    assert only.total_exec_time_ms == pytest.approx(450.0)

    both = run(_rank(limit=2))
    assert len(both.operations) == 2 and both.total_count == 4
    assert both.operations[0].source_name == "Other DB"
    # Shares are within each operation's own source.
    assert both.operations[0].share_of_total_time == pytest.approx(1.0)


# --- history ---------------------------------------------------------------------------------


def test_history_buckets_per_minute_with_zero_gaps(maker: Maker) -> None:
    source = add_source(maker)
    start = NOW.replace(second=0) - timedelta(minutes=10)
    op = add_operation(
        maker,
        source,
        "SELECT 1",
        [
            (start + timedelta(minutes=2, seconds=10), 3, 30.0),
            (start + timedelta(minutes=2, seconds=40), 1, 50.0),  # same minute
            (start + timedelta(minutes=5, seconds=5), 2, 4.0),
        ],
    )

    async def _history() -> Any:
        async with maker() as session:
            return await explorer.operation_history(
                session, op, Window(start, start + timedelta(minutes=8))
            )

    points = run(_history())
    assert len(points) == 8  # minutes 0..7: the minute starting at `end` isn't over
    assert [p.calls for p in points] == [0, 0, 4, 0, 0, 2, 0, 0]
    assert points[2].total_exec_time_ms == pytest.approx(80.0)
    assert points[2].mean_exec_time_ms == pytest.approx(20.0)
    assert points[0].mean_exec_time_ms is None
    assert points[2].start == start + timedelta(minutes=2)


def test_long_windows_bucket_per_hour(maker: Maker) -> None:
    source = add_source(maker)
    op = add_operation(
        maker, source, "SELECT 1", [(ago(60 * 5), 5, 5.0), (ago(60 * 5 + 1), 1, 1.0)]
    )
    window = Window.last(60 * 24, now=NOW)
    assert window.resolution == "hour" and window.step == timedelta(hours=1)

    async def _history() -> Any:
        async with maker() as session:
            return await explorer.operation_history(session, op, window)

    points = run(_history())
    assert len(points) == 25
    assert sum(p.calls for p in points) == 6
    assert all(p.start.minute == 0 for p in points)


# --- API -------------------------------------------------------------------------------------


def test_operations_api_lists_and_details(
    login: Callable[[str], TestClient], workload: dict[str, int]
) -> None:
    client = login("demo")
    listing = client.get("/api/operations", params={"minutes": 15, "sort": "calls"}).json()
    assert listing["sort"] == "calls"
    assert [o["id"] for o in listing["operations"]][0] == workload["chatty"]

    detail = client.get(f"/api/operations/{workload['slow']}", params={"minutes": 15}).json()
    assert detail["operation"]["calls"] == 10
    assert detail["source"]["id"] == workload["source"]
    assert detail["bucket_seconds"] == 60
    assert sum(p["calls"] for p in detail["history"]) == 10

    assert client.get("/api/operations/9999").status_code == 404


def test_absolute_windows_reopen_the_same_result(
    login: Callable[[str], TestClient], workload: dict[str, int]
) -> None:
    client = login("demo")
    params = {"start": ago(15).isoformat(), "end": NOW.isoformat()}
    first = client.get("/api/operations", params=params).json()
    again = client.get("/api/operations", params=params).json()
    assert first == again
    assert first["start"].startswith(ago(15).strftime("%Y-%m-%dT%H:%M"))


@pytest.mark.parametrize(
    "params",
    [
        {"minutes": 4},
        {"minutes": 60, "start": "2026-11-16T10:00:00Z"},
        {"end": "2026-11-16T10:00:00Z"},
        {"start": "2026-11-16T10:00:00Z", "end": "2026-11-16T10:00:30Z"},
        {"start": "2026-11-16T10:00:00Z", "end": "2026-11-16T09:00:00Z"},
        {"start": "2026-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
        {"sort": "random"},
        {"q": "x" * 201},
        {"limit": 0},
    ],
)
def test_bad_windows_and_filters_are_rejected(
    login: Callable[[str], TestClient], params: dict[str, Any]
) -> None:
    assert login("demo").get("/api/operations", params=params).status_code == 422


# --- plans -----------------------------------------------------------------------------------


def add_plan(maker: Maker, op: int, plan: list[Any] | None, error: str | None = None) -> int:
    async def _add() -> int:
        async with maker() as session:
            fp = await session.get(Fingerprint, op)
            assert fp is not None
            row = await plans.record_plan(session, fp, plan, error, datetime.now(UTC))
            await session.commit()
            return row.id

    return run(_add())


def test_plan_states(login: Callable[[str], TestClient], maker: Maker) -> None:
    client = login("demo")
    ok = add_source(maker, "Planned")
    op = add_operation(
        maker, ok, "SELECT id FROM orders WHERE customer_id = $1", [(ago(1), 1, 1.0)]
    )

    body = client.get(f"/api/operations/{op}/plan").json()
    assert body["status"] == "not_captured" and "collector" in body["reason"]

    add_plan(maker, op, None, "The monitoring user can't read a table")
    body = client.get(f"/api/operations/{op}/plan").json()
    assert body["status"] == "failed" and "can't read" in body["reason"]

    plan_id = add_plan(maker, op, plan_fixture("p1_orders_by_customer"))
    body = client.get(f"/api/operations/{op}/plan").json()
    assert body["status"] == "captured" and body["plan_id"] == plan_id
    assert body["raw"] == plan_fixture("p1_orders_by_customer")
    assert body["root"]["path"] == "0"

    old = add_source(maker, "Old PG", plans_ok=False)
    op_old = add_operation(maker, old, "SELECT 2", [(ago(1), 1, 1.0)])
    body = client.get(f"/api/operations/{op_old}/plan").json()
    assert body["status"] == "unavailable"
    assert body["fix"] == "Upgrade to PostgreSQL 16 or later."

    mongo = add_source(maker, "Mongo", engine=SourceEngine.MONGODB, plans_ok=None)
    op_mongo = add_operation(maker, mongo, "{find: ?}", [(ago(1), 1, 1.0)])
    assert client.get(f"/api/operations/{op_mongo}/plan").json()["status"] == "unavailable"


def test_unchanged_plan_is_rechecked_not_duplicated(maker: Maker) -> None:
    source = add_source(maker)
    op = add_operation(maker, source, "SELECT 1", [(ago(1), 1, 1.0)])
    plan = plan_fixture("c1_customer_by_id")
    first = add_plan(maker, op, plan)
    assert add_plan(maker, op, plan) == first, "same plan: same row, re-checked"
    changed = add_plan(maker, op, plan_fixture("p2_latest_orders"))
    assert changed != first, "a changed plan is new evidence: a new row"

    async def _count() -> int:
        async with maker() as session:
            return int(await session.scalar(select(func.count()).select_from(Plan)) or 0)

    assert run(_count()) == 2


class FakePlanner:
    """Stands in for PostgresAdapter: connects to nothing and returns a canned plan."""

    def __init__(self, plan: list[Any]) -> None:
        self.plan = plan
        self.explained: list[str] = []

    async def connect(self, params: Any) -> Any:
        class Conn:
            async def __aenter__(self) -> Conn:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        return Conn()

    async def explain_generic(self, conn: Any, query: str, username: str) -> list[Any]:
        self.explained.append(query)
        return self.plan


def test_admin_can_recapture_a_plan(
    client: TestClient, login: Callable[[str], TestClient], maker: Maker, settings: Settings
) -> None:
    source = add_source(maker)
    op = add_operation(
        maker, source, "SELECT id FROM orders WHERE customer_id = $1", [(ago(1), 1, 1.0)]
    )

    async def _password() -> None:
        async with maker() as session:
            row = await session.get(DataSource, source)
            assert row is not None
            row.password_encrypted = CredentialCipher(settings.fernet_keys).encrypt("pw")
            await session.commit()

    run(_password())
    fake = FakePlanner(plan_fixture("p1_orders_by_customer"))
    client.app.state.postgres_adapter = fake  # type: ignore[attr-defined]
    body = login("admin").post(f"/api/operations/{op}/plan").json()
    assert body["status"] == "captured"
    assert fake.explained == ["SELECT id FROM orders WHERE customer_id = $1"]
    again = client.post(f"/api/operations/{op}/plan").json()
    assert again["plan_id"] == body["plan_id"]


def test_top_fingerprints_and_planned_ids(maker: Maker, workload: dict[str, int]) -> None:
    async def _check() -> tuple[list[int], set[int]]:
        async with maker() as session:
            top = await plans.top_fingerprints(session, workload["source"], ago(15), limit=2)
            fp = await session.get(Fingerprint, workload["slow"])
            assert fp is not None
            await plans.record_plan(session, fp, None, "x", datetime.now(UTC))
            done = await plans.planned_ids(session, [f.id for f in top])
            return [f.id for f in top], done

    top, done = run(_check())
    assert top == [workload["slow"], workload["spiky"]]
    assert done == {workload["slow"]}


def test_retention_removes_plans_not_rechecked(maker: Maker, settings: Settings) -> None:
    source = add_source(maker)
    op = add_operation(maker, source, "SELECT 1", [(ago(1), 1, 1.0)])

    async def _scenario() -> list[str | None]:
        stale = datetime.now(UTC) - timedelta(days=settings.retention_days + 1)
        async with maker() as session:
            for error, checked in (("stale", stale), ("fresh", datetime.now(UTC))):
                session.add(
                    Plan(
                        fingerprint_id=op,
                        source_id=source,
                        engine="postgres",
                        plan=None,
                        error=error,
                        content_hash=error,
                        captured_at=stale,
                        checked_at=checked,
                    )
                )
            await session.commit()
        await Collector(settings, maker, CredentialCipher(settings.fernet_keys)).purge_old_data()
        async with maker() as session:
            return list(await session.scalars(select(Plan.error)))

    assert run(_scenario()) == ["fresh"]
