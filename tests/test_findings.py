"""Findings (Phase 5): every rule with fixture evidence, the engine's lifecycle, and the API.

The rules run on real plans captured from the demo database (tests/fixtures/plans/). Each
planted problem's rule must fire on it, and stay silent on its healthy control (QP-AC-18 and
QP-AC-19 at unit level; tests/acceptance proves them on the running stack). Dismissal and
evidence links (QP-AC-20, QP-AC-21) are tested through the engine and the API here, and
across real collection cycles in tests/integration/test_findings_cycles.py.
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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.config import Settings
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.findings import engine
from query_pulse.findings.models import Finding
from query_pulse.findings.rules import (
    RULES_BY_ID,
    IndexHistory,
    IndexSample,
    OperationFacts,
    PlanFacts,
    RuleContext,
    TableFacts,
    run_rules,
)
from query_pulse.sources.models import DataSource, SourceEngine, SourceStatus
from query_pulse.workload.models import Fingerprint, Observation, Plan, RelationStat
from query_pulse.workload.plan_tree import parse_plan

FIXTURES = Path(__file__).parent / "fixtures" / "plans"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
Maker = async_sessionmaker[AsyncSession]


def raw_plan(name: str) -> list[Any]:
    data: list[Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def plan(name: str, plan_id: int = 1) -> PlanFacts:
    return PlanFacts(plan_id=plan_id, root=parse_plan(raw_plan(name)), checked_at=NOW)


def op(fid: int, query: str = "q", calls: int = 1000, share: float = 0.5) -> OperationFacts:
    return OperationFacts(fid, query, calls, 1000.0 * share, share)


def table(name: str, rows: int, stat_id: int = 100) -> TableFacts:
    return TableFacts(stat_id, "public", name, rows, rows * 100, NOW)


def index(
    name: str,
    scans: list[int] | None = None,
    unique: bool | None = False,
    size: int = 8 * 1024 * 1024,
    span: timedelta = timedelta(hours=2),
) -> IndexHistory:
    scans = scans if scans is not None else [0, 0, 0]
    step = span / max(1, len(scans) - 1)
    return IndexHistory(
        "public",
        "orders",
        name,
        unique,
        [IndexSample(200 + i, NOW - span + step * i, n, size) for i, n in enumerate(scans)],
    )


def context(**kwargs: Any) -> RuleContext:
    defaults: dict[str, Any] = {
        "source_id": 1,
        "window_start": NOW - timedelta(hours=1),
        "window_end": NOW,
        "tables": [
            table("orders", 200_000, 101),
            table("products", 5_000, 102),
            table("customers", 20_000, 103),
        ],
    }
    defaults.update(kwargs)
    return RuleContext(**defaults)


def fired(ctx: RuleContext) -> list[tuple[str, str]]:
    return sorted((d.rule_id, d.target_key) for d in run_rules(ctx))


# --- PG-SEQSCAN-HOT (P1) ---------------------------------------------------------------------


def test_p1_hot_seq_scan_fires_with_its_evidence_and_fix() -> None:
    ctx = context(operations=[op(1, share=0.54)], plans={1: plan("p1_orders_by_customer", 7)})
    [d] = run_rules(ctx)
    assert (d.rule_id, d.target_key, d.confidence) == ("PG-SEQSCAN-HOT", "operation:1", "high")
    assert d.action_sql == "CREATE INDEX CONCURRENTLY ON public.orders (customer_id);"
    assert "54%" in d.summary and "200,000" in d.summary
    kinds = {e["kind"]: e for e in d.evidence}
    assert kinds["plan"]["plan_id"] == 7 and kinds["plan"]["node_path"] == "0"
    assert kinds["relation_stat"]["relation_stat_id"] == 101
    assert kinds["operation"]["fingerprint_id"] == 1
    facts = {f["key"]: f["value"] for f in d.facts}
    assert facts["table_rows"] == 200_000 and facts["selectivity"] < 0.001


@pytest.mark.parametrize(
    ("operation", "tables", "why"),
    [
        (op(1, share=0.05), None, "not hot: 5% of the time"),
        (op(1, calls=3), None, "too few calls"),
        (op(1), [table("orders", 5_000)], "small table"),
    ],
)
def test_p1_rule_stays_silent_when_a_condition_fails(
    operation: OperationFacts, tables: list[TableFacts] | None, why: str
) -> None:
    kwargs: dict[str, Any] = {
        "operations": [operation],
        "plans": {1: plan("p1_orders_by_customer")},
    }
    if tables is not None:
        kwargs["tables"] = tables
    assert fired(context(**kwargs)) == [], why


def test_seq_scan_keeping_much_of_the_table_is_not_flagged() -> None:
    # 833 of 5,000 products (17%): a full scan is the right plan, even if the query is hot.
    nonselective = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "orders",
                "Total Cost": 4443.0,
                "Plan Rows": 40_000,
                "Plan Width": 20,
                "Filter": "(status = $1)",
            }
        }
    ]
    ctx = context(
        operations=[op(1)],
        plans={1: PlanFacts(1, parse_plan(nonselective), NOW)},
    )
    assert fired(ctx) == []


def test_ambiguous_table_names_give_no_answer() -> None:
    other = TableFacts(999, "archive", "orders", 300_000, 1, NOW)
    ctx = context(
        operations=[op(1)],
        plans={1: plan("p1_orders_by_customer")},
        tables=[table("orders", 200_000), other],
    )
    assert fired(ctx) == [], "orders exists in two schemas; the plan doesn't say which"


# --- PG-SORT-NO-INDEX (P2) -------------------------------------------------------------------


def test_p2_sort_under_limit_fires_once_and_seqscan_rule_does_not() -> None:
    # Hot as well, but its scan has no filter: that's the sort rule's problem, not P1's.
    ctx = context(operations=[op(2, share=0.4)], plans={2: plan("p2_latest_orders", 8)})
    [d] = run_rules(ctx)
    assert d.rule_id == "PG-SORT-NO-INDEX"
    assert d.action_sql == "CREATE INDEX CONCURRENTLY ON public.orders (created_at DESC);"
    [cited] = [e for e in d.evidence if e["kind"] == "plan"]
    sort = parse_plan(raw_plan("p2_latest_orders")).children[0].children[0]
    assert sort.node_type == "Sort" and cited["node_path"] == sort.path


def test_sort_without_limit_is_not_flagged() -> None:
    no_limit = raw_plan("p2_latest_orders")[0]["Plan"]["Plans"][0]  # drop the Limit
    ctx = context(
        operations=[op(2)], plans={2: PlanFacts(2, parse_plan([{"Plan": no_limit}]), NOW)}
    )
    assert fired(ctx) == []


# --- PG-LEADING-WILDCARD (P3) ----------------------------------------------------------------


def test_p3_parameter_pattern_fires_with_medium_confidence() -> None:
    ctx = context(operations=[op(3, share=0.6)], plans={3: plan("p3_product_search", 9)})
    [d] = run_rules(ctx)
    assert (d.rule_id, d.confidence) == ("PG-LEADING-WILDCARD", "medium")
    assert "isn't recorded" in d.summary, "says honestly that the pattern is inferred"
    assert d.action_sql is not None and "gin (name gin_trgm_ops)" in d.action_sql


def like_plan(pattern: str) -> PlanFacts:
    document = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "products",
                "Total Cost": 110.5,
                "Plan Rows": 25,
                "Plan Width": 31,
                "Filter": f"(name ~~ {pattern})",
            }
        }
    ]
    return PlanFacts(1, parse_plan(document), NOW)


def test_visible_leading_wildcard_is_high_confidence() -> None:
    [d] = run_rules(context(operations=[op(3)], plans={3: like_plan("'%lamp%'::text")}))
    assert d.confidence == "high"


def test_anchored_literal_pattern_is_not_flagged() -> None:
    assert fired(context(operations=[op(3)], plans={3: like_plan("'lamp%'::text")})) == []


# --- PG-UNUSED-INDEX (P4) --------------------------------------------------------------------


def test_p4_unused_index_fires() -> None:
    [d] = run_rules(context(indexes=[index("idx_orders_legacy_lookup")]))
    assert d.rule_id == "PG-UNUSED-INDEX" and d.confidence == "medium"
    assert d.target_key == "index:public.idx_orders_legacy_lookup"
    assert d.action_sql == "DROP INDEX CONCURRENTLY public.idx_orders_legacy_lookup;"
    assert "replicas" in d.summary and "statistics reset" in d.summary
    assert [e["relation_stat_id"] for e in d.evidence] == [200, 202]


@pytest.mark.parametrize(
    ("history", "why"),
    [
        (index("customers_email_key", unique=True), "UNIQUE indexes enforce constraints"),
        (index("x", unique=None), "uniqueness unknown (old samples): skip, don't guess"),
        (index("orders_pkey", scans=[0, 0, 5]), "used during the window"),
        (index("x", span=timedelta(minutes=30)), "not watched long enough"),
        (index("idx_products_category", size=64 * 1024), "too small to matter"),
        (index("x", scans=[0]), "a single sample"),
    ],
)
def test_unused_index_rule_stays_silent(history: IndexHistory, why: str) -> None:
    assert fired(context(indexes=[history])) == [], why


# --- healthy controls (QP-AC-19) and one finding per problem (QP-AC-18) ----------------------


def test_demo_workload_gives_exactly_one_finding_per_planted_problem() -> None:
    """P1-P4 plus controls C1 (customer by id), C3 (category, below) and the indexes the
    controls use: four findings, one per planted problem, from its expected rule."""
    c3_seq = [
        {
            "Plan": {
                "Node Type": "Limit",
                "Total Cost": 11.0,
                "Plan Rows": 83,
                "Plan Width": 31,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "products",
                        "Total Cost": 110.5,
                        "Plan Rows": 833,
                        "Plan Width": 31,
                        "Filter": "(category = $1)",
                    }
                ],
            }
        }
    ]
    ctx = context(
        operations=[
            op(1, share=0.5),
            op(2, share=0.4),
            op(3, share=0.02),
            op(11, share=0.01),
            op(13, share=0.01),
        ],
        plans={
            1: plan("p1_orders_by_customer", 1),
            2: plan("p2_latest_orders", 2),
            3: plan("p3_product_search", 3),
            11: plan("c1_customer_by_id", 11),
            13: PlanFacts(13, parse_plan(c3_seq), NOW),
        },
        indexes=[
            index("idx_orders_legacy_lookup"),
            index("customers_email_key", unique=True),
            index("orders_pkey", scans=[100, 200, 300], unique=True),
            index("idx_products_category", scans=[10, 20, 30]),
        ],
    )
    assert fired(ctx) == [
        ("PG-LEADING-WILDCARD", "operation:3"),
        ("PG-SEQSCAN-HOT", "operation:1"),
        ("PG-SORT-NO-INDEX", "operation:2"),
        ("PG-UNUSED-INDEX", "index:public.idx_orders_legacy_lookup"),
    ]


def test_every_rule_is_declared_with_its_evidence_kind() -> None:
    assert set(RULES_BY_ID) == {
        "PG-SEQSCAN-HOT",
        "PG-SORT-NO-INDEX",
        "PG-LEADING-WILDCARD",
        "PG-UNUSED-INDEX",
    }
    assert RULES_BY_ID["PG-UNUSED-INDEX"].evidence_strength == "statistics"
    assert {r.evidence_strength for r in RULES_BY_ID.values() if r.id != "PG-UNUSED-INDEX"} == {
        "plan"
    }


# --- the engine against the app database -----------------------------------------------------


@pytest.fixture
def maker(database_url: str) -> Iterator[Maker]:
    db = create_engine(database_url)
    yield create_sessionmaker(db)
    asyncio.run(db.dispose())


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def seed_demo(maker: Maker, now: datetime) -> dict[str, int]:
    """A source with P1-P3 and C1 operations, their plans, and table and index samples."""

    async def _seed() -> dict[str, int]:
        async with maker() as session:
            source = DataSource(
                name="Demo",
                engine=SourceEngine.POSTGRES,
                host="h",
                port=5432,
                database="shop",
                username="monitor",
                tls_mode="disable",
                enabled=True,
                description="",
                status=SourceStatus.HEALTHY,
            )
            session.add(source)
            await session.flush()
            ids: dict[str, int] = {"source": source.id}
            for key, query, total, fixture in (
                (
                    "p1",
                    "SELECT id FROM orders WHERE customer_id = $1",
                    500.0,
                    "p1_orders_by_customer",
                ),
                ("p2", "SELECT ... ORDER BY created_at DESC LIMIT $1", 400.0, "p2_latest_orders"),
                ("p3", "SELECT ... WHERE name LIKE $1 LIMIT $2", 50.0, "p3_product_search"),
                ("c1", "SELECT ... FROM customers WHERE id = $1", 50.0, "c1_customer_by_id"),
            ):
                fp = Fingerprint(
                    source_id=source.id,
                    dbid=1,
                    userid=1,
                    queryid=hash(key) % 10**9,
                    toplevel=True,
                    query=query,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(fp)
                await session.flush()
                ids[key] = fp.id
                session.add(
                    Observation(
                        fingerprint_id=fp.id,
                        source_id=source.id,
                        interval_start=now - timedelta(minutes=2),
                        interval_end=now - timedelta(minutes=1),
                        calls=100,
                        total_exec_time_ms=total,
                        mean_exec_time_ms=total / 100,
                        rows=100,
                        shared_blks_hit=0,
                        shared_blks_read=0,
                    )
                )
                session.add(
                    Plan(
                        fingerprint_id=fp.id,
                        source_id=source.id,
                        engine="postgres",
                        plan=raw_plan(fixture),
                        error=None,
                        content_hash=key,
                        captured_at=now,
                        checked_at=now,
                    )
                )
            for name, rows in (("orders", 200_000), ("products", 5_000), ("customers", 20_000)):
                session.add(
                    RelationStat(
                        source_id=source.id,
                        captured_at=now,
                        kind="table",
                        schema_name="public",
                        table_name=name,
                        live_rows=rows,
                        size_bytes=rows * 100,
                    )
                )
            for when in (now - timedelta(hours=2), now - timedelta(minutes=1)):
                for name, scans, unique in (
                    ("idx_orders_legacy_lookup", 0, False),
                    ("customers_email_key", 0, True),
                ):
                    session.add(
                        RelationStat(
                            source_id=source.id,
                            captured_at=when,
                            kind="index",
                            schema_name="public",
                            table_name="orders",
                            index_name=name,
                            idx_scan=scans,
                            size_bytes=8 * 1024 * 1024,
                            is_unique=unique,
                        )
                    )
            await session.commit()
            return ids

    return run(_seed())


def evaluate(maker: Maker, settings: Settings, source_id: int, now: datetime) -> engine.Reconciled:
    async def _go() -> engine.Reconciled:
        async with maker() as session:
            result = await engine.evaluate_source(session, source_id, settings, now)
            await session.commit()
            return result

    return run(_go())


def findings(maker: Maker) -> dict[str, Finding]:
    async def _load() -> dict[str, Finding]:
        async with maker() as session:
            return {f.rule_id: f for f in await session.scalars(select(Finding))}

    return run(_load())


def set_status(maker: Maker, finding_id: int, status: str, at: datetime) -> None:
    async def _set() -> None:
        async with maker() as session:
            finding = await session.get(Finding, finding_id)
            assert finding is not None
            finding.status = status
            finding.status_changed_by = "user"
            finding.resolved_at = at if status == "resolved" else None
            await session.commit()

    run(_set())


def test_engine_creates_one_finding_per_problem_and_never_duplicates(
    maker: Maker, settings: Settings
) -> None:
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)
    assert evaluate(maker, settings, ids["source"], now) == engine.Reconciled(created=4)
    again = evaluate(maker, settings, ids["source"], now + timedelta(seconds=15))
    assert again == engine.Reconciled(updated=4)
    found = findings(maker)
    assert len(found) == 4
    assert found["PG-SEQSCAN-HOT"].fingerprint_id == ids["p1"]
    assert found["PG-SORT-NO-INDEX"].fingerprint_id == ids["p2"]
    assert found["PG-LEADING-WILDCARD"].fingerprint_id == ids["p3"]
    assert found["PG-UNUSED-INDEX"].relation == "public.idx_orders_legacy_lookup"
    assert all(f.status == "open" for f in found.values())


def test_dismissed_finding_stays_dismissed(maker: Maker, settings: Settings) -> None:
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)
    evaluate(maker, settings, ids["source"], now)
    p1 = findings(maker)["PG-SEQSCAN-HOT"]
    set_status(maker, p1.id, "dismissed", now)
    for i in range(1, 4):
        evaluate(maker, settings, ids["source"], now + timedelta(seconds=15 * i))
    after = findings(maker)["PG-SEQSCAN-HOT"]
    assert after.status == "dismissed" and after.id == p1.id
    assert after.last_seen_at > p1.last_seen_at, "still detected, still tracked"


def test_resolved_finding_reopens_only_on_newer_evidence(maker: Maker, settings: Settings) -> None:
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)
    evaluate(maker, settings, ids["source"], now)
    p1 = findings(maker)["PG-SEQSCAN-HOT"]
    # Resolved in the future relative to all evidence: the old plan doesn't reopen it.
    set_status(maker, p1.id, "resolved", now + timedelta(hours=1))
    evaluate(maker, settings, ids["source"], now + timedelta(minutes=1))
    assert findings(maker)["PG-SEQSCAN-HOT"].status == "resolved"
    # Resolved before the evidence was collected: the problem is still there, reopen.
    set_status(maker, p1.id, "resolved", now - timedelta(hours=1))
    assert evaluate(maker, settings, ids["source"], now + timedelta(minutes=2)).reopened == 1
    assert findings(maker)["PG-SEQSCAN-HOT"].status == "open"


def test_open_findings_resolve_once_the_problem_is_gone(maker: Maker, settings: Settings) -> None:
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)
    evaluate(maker, settings, ids["source"], now)

    async def _index_added() -> None:  # P1's plan becomes an index scan
        async with maker() as session:
            await session.execute(delete(Plan).where(Plan.fingerprint_id == ids["p1"]))
            await session.commit()

    run(_index_added())
    soon = evaluate(maker, settings, ids["source"], now + timedelta(minutes=5))
    assert soon.resolved == 0, "not seen for 5 minutes isn't long enough"
    later = now + timedelta(seconds=settings.finding_resolve_after_seconds + 60)
    assert evaluate(maker, settings, ids["source"], later).resolved >= 1
    assert findings(maker)["PG-SEQSCAN-HOT"].status == "resolved"


# --- API ------------------------------------------------------------------------------------


def test_findings_api_lists_filters_and_resolves_every_evidence_link(
    login: Callable[[str], TestClient], maker: Maker, settings: Settings
) -> None:
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)
    evaluate(maker, settings, ids["source"], now)
    client = login("demo")

    body = client.get("/api/findings").json()
    assert body["counts"] == {"open": 4, "resolved": 0, "dismissed": 0}
    assert len(body["findings"]) == 4
    assert [f["confidence"] for f in body["findings"]][:2] == ["high", "high"]

    # QP-AC-20: every evidence link resolves to an existing observation window, plan or sample.
    for finding in body["findings"]:
        assert finding["evidence"], finding["rule_id"]
        for item in finding["evidence"]:
            assert item["available"], (finding["rule_id"], item)
            if item["kind"] == "plan":
                cited = client.get(f"/api/plans/{item['plan_id']}")
                assert cited.status_code == 200 and cited.json()["status"] == "captured"
            if item["kind"] == "operation":
                assert client.get(f"/api/operations/{item['fingerprint_id']}").status_code == 200

    [p1] = client.get("/api/findings", params={"operation_id": ids["p1"]}).json()["findings"]
    assert p1["rule_id"] == "PG-SEQSCAN-HOT" and p1["query"].startswith("SELECT id FROM orders")
    assert p1["evidence_strength"] == "plan"
    assert {f["key"] for f in p1["facts"]} >= {"share_of_total_time", "table_rows"}
    assert (
        client.get("/api/findings", params={"rule_id": "PG-UNUSED-INDEX"}).json()["findings"][0][
            "evidence_strength"
        ]
        == "statistics"
    )
    assert client.get(f"/api/findings/{p1['id']}").json()["id"] == p1["id"]
    assert client.get("/api/findings/9999").status_code == 404
    assert client.get("/api/plans/9999").status_code == 404


def test_admin_dismisses_and_reopens_a_finding(
    login: Callable[[str], TestClient], maker: Maker, settings: Settings
) -> None:
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)
    evaluate(maker, settings, ids["source"], now)
    client = login("admin")
    finding = client.get("/api/findings").json()["findings"][0]

    dismissed = client.patch(f"/api/findings/{finding['id']}", json={"status": "dismissed"}).json()
    assert (dismissed["status"], dismissed["status_changed_by"]) == ("dismissed", "user")
    counts = client.get("/api/findings", params={"status": "open"}).json()["counts"]
    assert counts == {"open": 3, "resolved": 0, "dismissed": 1}

    resolved = client.patch(f"/api/findings/{finding['id']}", json={"status": "resolved"}).json()
    assert resolved["resolved_at"] is not None
    reopened = client.patch(f"/api/findings/{finding['id']}", json={"status": "open"}).json()
    assert reopened["status"] == "open" and reopened["resolved_at"] is None
    assert (
        client.patch(f"/api/findings/{finding['id']}", json={"status": "gone"}).status_code == 422
    )


def test_evidence_reports_itself_unavailable_after_retention(
    login: Callable[[str], TestClient], maker: Maker, settings: Settings
) -> None:
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)
    evaluate(maker, settings, ids["source"], now)

    async def _age_out() -> None:
        async with maker() as session:
            await session.execute(delete(Plan).where(Plan.fingerprint_id == ids["p1"]))
            await session.commit()

    run(_age_out())
    [p1] = login("demo").get("/api/findings", params={"operation_id": ids["p1"]}).json()["findings"]
    plan_link = next(e for e in p1["evidence"] if e["kind"] == "plan")
    assert plan_link["available"] is False
    assert {f["key"] for f in p1["facts"]} >= {"table_rows"}, "the facts are kept"


def test_rule_catalogue(login: Callable[[str], TestClient]) -> None:
    rules = login("demo").get("/api/rules").json()
    assert [r["id"] for r in rules] == [
        "PG-SEQSCAN-HOT",
        "PG-SORT-NO-INDEX",
        "PG-LEADING-WILDCARD",
        "PG-UNUSED-INDEX",
    ]
    assert rules[0]["thresholds"]["min_share_of_total_time"] == 0.1


def test_rules_do_not_run_for_a_skipped_source(maker: Maker, settings: Settings) -> None:
    """A disabled source has no new evidence; its findings must not be judged on old data."""
    from query_pulse.collector.cycle import Collector
    from query_pulse.collector.models import CollectorState
    from query_pulse.crypto import CredentialCipher

    now = datetime.now(UTC)
    ids = seed_demo(maker, now)

    async def _disable() -> None:
        async with maker() as session:
            source = await session.get(DataSource, ids["source"])
            assert source is not None
            source.enabled = False
            session.add(
                CollectorState(source_id=source.id, consecutive_failures=0, evicted_statements=0)
            )
            await session.commit()

    run(_disable())
    collector = Collector(settings, maker, CredentialCipher(settings.fernet_keys))
    assert run(collector.collect(ids["source"])).skipped
    assert findings(maker) == {}
    assert run(collector.evaluate_rules(ids["source"], force=True)), "callable directly"
    assert len(findings(maker)) == 4
