"""MongoDB (Phase 6) without a server: shapes, plan redaction, rules M1-M3, profile grouping.

- QP-AC-22: golden files (tests/golden/mongo_shapes/) pin the normaliser's output, and the
  pair tests prove the rule itself: values never split a shape, fields and operators do.
- Plans: real explain output captured from MongoDB 7 (tests/fixtures/mongo_plans/) is
  redacted so no query value survives, then read into stages.
- QP-AC-23 at unit level: each planted problem's rule fires on its real plan, and the
  controls stay silent. tests/acceptance proves it on the running stack.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from bson import json_util

from query_pulse.adapters.mongodb import ProfileEntry
from query_pulse.collector.mongo import SampleCache, group_profile
from query_pulse.findings.rules import OperationFacts, PlanFacts, RuleContext, run_rules
from query_pulse.workload import mongo_plan
from query_pulse.workload.mongo_shape import (
    NotExplainableError,
    explain_command,
    shape_of,
)

GOLDEN = Path(__file__).parent / "golden" / "mongo_shapes"
PLANS = Path(__file__).parent / "fixtures" / "mongo_plans"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


# --- QP-AC-22: the shape normaliser -----------------------------------------------------------


def _bson(document: dict[str, Any]) -> Any:
    """Extended JSON to BSON types, except {"$regex": ...}: in a profiled command that's the
    query operator (a plain document), while json_util would read it as a BSON Regex."""
    return document if "$regex" in document else json_util.object_hook(document)


def golden_cases() -> list[Path]:
    return sorted(GOLDEN.glob("*.json"))


@pytest.mark.parametrize("path", golden_cases(), ids=lambda p: p.stem)
def test_golden_shapes(path: Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    # Extended JSON gives the real BSON types ($oid -> ObjectId, $date -> datetime, ...).
    command = json.loads(json.dumps(case["command"]), object_hook=_bson)
    shape = shape_of(case["op"], case["ns"], command)
    assert shape is not None, case["description"]
    assert shape.document == case["shape"], case["description"]


def test_there_are_golden_files_for_every_command() -> None:
    commands = set()
    for path in golden_cases():
        commands.add(next(iter(json.loads(path.read_text(encoding="utf-8"))["shape"])))
    assert commands >= {
        "find",
        "aggregate",
        "count",
        "distinct",
        "findAndModify",
        "update",
        "delete",
        "insert",
    }


def find(filter_: dict[str, Any], **extra: Any) -> str:
    shape = shape_of("query", "shop.orders", {"find": "orders", "filter": filter_, **extra})
    assert shape is not None
    return shape.hash


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"customerId": 42}, {"customerId": 7}),
        ({"orderNo": {"$in": [1, 2, 3]}}, {"orderNo": {"$in": [9]}}),
        ({"sku": {"$regex": "1234"}}, {"sku": {"$regex": "^SKU-9"}}),
        ({"status": "paid", "total": {"$gt": 10}}, {"status": "new", "total": {"$gt": 999.5}}),
        ({"placedAt": {"$gte": NOW}}, {"placedAt": {"$gte": NOW - timedelta(days=3)}}),
        ({"$or": [{"a": 1}, {"b": "x"}]}, {"$or": [{"a": 2}, {"b": "y"}]}),
        ({"coupon": None}, {"coupon": "SAVE"}),
    ],
)
def test_queries_differing_only_in_values_share_a_shape(
    first: dict[str, Any], second: dict[str, Any]
) -> None:
    assert find(first) == find(second)


def test_limit_skip_and_session_metadata_never_split_a_shape() -> None:
    a = find({}, sort={"placedAt": -1}, limit=20, lsid={"id": 1}, **{"$db": "shop"})
    b = find({}, sort={"placedAt": -1}, limit=50, lsid={"id": 2}, **{"$db": "other"})
    assert a == b


@pytest.mark.parametrize(
    ("first", "second", "why"),
    [
        ({"customerId": 42}, {"orderNo": 42}, "a different field"),
        ({"total": {"$gt": 10}}, {"total": {"$gte": 10}}, "a different operator"),
        ({"a": 1}, {"a": {"$in": [1]}}, "equality vs $in"),
        ({"a": 1}, {"a": 1, "b": 2}, "an extra field"),
        ({"$or": [{"a": 1}]}, {"$and": [{"a": 1}]}, "a different logical operator"),
    ],
)
def test_different_fields_or_operators_give_different_shapes(
    first: dict[str, Any], second: dict[str, Any], why: str
) -> None:
    assert find(first) != find(second), why


def test_sort_collection_and_command_are_part_of_the_shape() -> None:
    assert find({}, sort={"placedAt": -1}) != find({}, sort={"placedAt": 1})
    other = shape_of("query", "shop.archive", {"find": "archive", "filter": {}})
    count = shape_of("command", "shop.orders", {"count": "orders", "query": {}})
    assert other is not None and count is not None
    assert len({find({}), other.hash, count.hash}) == 3


@pytest.mark.parametrize(
    ("op", "ns", "command"),
    [
        ("getmore", "shop.orders", {"getMore": 1, "collection": "orders"}),
        ("command", "shop.$cmd", {"hello": 1}),
        ("command", "shop.orders", {"createIndexes": "orders", "indexes": []}),
        ("query", "shop.system.profile", {"find": "system.profile", "filter": {}}),
        ("query", "noDot", {"find": "x"}),
    ],
)
def test_administration_and_continuations_are_not_fingerprinted(
    op: str, ns: str, command: dict[str, Any]
) -> None:
    assert shape_of(op, ns, command) is None


def test_shape_text_is_compact_json_without_values() -> None:
    shape = shape_of("query", "shop.orders", {"find": "orders", "filter": {"customerId": 42}})
    assert shape is not None
    assert shape.text == '{"find": "orders", "filter": {"customerId": "?"}}'
    assert (shape.namespace, shape.command, shape.is_read) == ("shop.orders", "find", True)


# --- explain commands ------------------------------------------------------------------------


def test_explain_command_replays_only_the_query_fields() -> None:
    command = {"find": "orders", "filter": {"customerId": 42}, "limit": 5, "lsid": {"id": 1}}
    shape = shape_of("query", "shop.orders", command)
    assert shape is not None
    assert explain_command(shape, command) == {
        "find": "orders",
        "filter": {"customerId": 42},
        "limit": 5,
    }


@pytest.mark.parametrize(
    ("op", "command", "reason"),
    [
        ("update", {"q": {"a": 1}, "u": {"$set": {"b": 2}}}, "reads only"),
        ("command", {"aggregate": "orders", "pipeline": [{"$out": "copy"}]}, "$out or $merge"),
        ("query", {"$truncated": "..."}, "truncated"),
    ],
)
def test_writes_and_truncated_commands_are_not_explained(
    op: str, command: dict[str, Any], reason: str
) -> None:
    shape = shape_of(op, "shop.orders", command)
    assert shape is not None
    with pytest.raises(NotExplainableError, match=reason.replace("$", r"\$")):
        explain_command(shape, command)


def test_aggregate_explain_gets_a_cursor() -> None:
    command = {
        "aggregate": "orders",
        "pipeline": [{"$match": {"a": 1}}],
        "cursor": {"batchSize": 1},
    }
    shape = shape_of("command", "shop.orders", command)
    assert shape is not None
    assert explain_command(shape, command)["cursor"] == {}


# --- plans: redaction and parsing ------------------------------------------------------------


def explain(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((PLANS / f"{name}.json").read_text(encoding="utf-8"))
    return data


def stored(name: str) -> list[Any]:
    return [mongo_plan.redact(explain(name))]


@pytest.mark.parametrize("name", sorted(p.stem for p in PLANS.glob("*.json")))
def test_redaction_leaves_no_query_value(name: str) -> None:
    raw = explain(name)
    values = _values(raw["command"])
    text = json.dumps(stored(name))
    for value in values:
        token = re.escape(json.dumps(value))
        found = re.search(rf"(?<![\w.]){token}(?![\w.])", text)
        assert found is None, f"{value!r} survived redaction in {name}"
    assert "slotBasedPlan" not in text and "command" not in json.loads(text)[0]


def _values(command: dict[str, Any]) -> list[Any]:
    """The literal values in a captured command's filter (what must never be stored)."""
    found: list[Any] = []

    def visit(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k not in ("sort", "find", "aggregate", "$db"):
                    visit(v, k)
        elif isinstance(node, list):
            for item in node:
                visit(item, key)
        elif (
            key not in ("limit",)
            and isinstance(node, str | int | float)
            and node
            not in (
                1,
                -1,
            )
        ):
            found.append(node)

    visit(command.get("filter") or command.get("pipeline") or {})
    return found


def test_bounds_keep_only_full_ranges() -> None:
    root = mongo_plan.parse_plan(stored("regex_unanchored"))
    ixscan = next(s for s in mongo_plan.walk(root) if s.stage == "IXSCAN")
    assert ixscan.index_bounds == {"sku": ['["", {})', "?"]}
    assert ixscan.full_index_scan is True
    assert ixscan.filter == '{"sku": {"$regex": "?"}}'
    anchored = mongo_plan.parse_plan(stored("regex_anchored"))
    tight = next(s for s in mongo_plan.walk(anchored) if s.stage == "IXSCAN")
    assert tight.index_bounds == {"sku": ["?", "?"]} and tight.full_index_scan is False


def test_parsed_plan_has_positional_paths_and_structure() -> None:
    root = mongo_plan.parse_plan(stored("sort_no_index"))
    assert (root.path, root.stage, root.sort_pattern) == ("0", "SORT", '{"placedAt": -1}')
    assert root.details["limitAmount"] == "?" and root.details["memLimit"] == "104857600"
    [child] = root.children
    assert (child.path, child.stage, child.filter) == ("0.0", "COLLSCAN", None)


def test_aggregate_plan_comes_from_the_cursor_stage() -> None:
    document = stored("aggregate_group")
    root = mongo_plan.parse_plan(document)
    assert [s.stage for s in mongo_plan.walk(root)] == ["PROJECTION_SIMPLE", "COLLSCAN"]
    assert mongo_plan.pipeline_stages(document) == ["$group", "$sort"]
    assert mongo_plan.rejected_count(document) == 0


def test_sbe_query_plan_is_unwrapped() -> None:
    sbe = {
        "queryPlanner": {
            "namespace": "shop.orders",
            "parsedQuery": {"a": {"$eq": 5}},
            "winningPlan": {
                "queryPlan": {"stage": "COLLSCAN", "filter": {"a": {"$eq": 5}}},
                "slotBasedPlan": {"stages": "[1] scan s1 ... a == 5"},
            },
            "rejectedPlans": [],
        }
    }
    document = [mongo_plan.redact(sbe)]
    assert "a == 5" not in json.dumps(document)
    assert mongo_plan.parse_plan(document).filter == '{"a": {"$eq": "?"}}'


def test_unreadable_documents_raise() -> None:
    with pytest.raises(mongo_plan.MongoPlanFormatError):
        mongo_plan.redact({"ok": 1})
    with pytest.raises(mongo_plan.MongoPlanFormatError):
        mongo_plan.parse_plan([{"winningPlan": {"no": "stage"}}])
    with pytest.raises(mongo_plan.MongoPlanFormatError):
        mongo_plan.parse_plan({"not": "a list"})


# --- rules M1-M3 (QP-AC-23 at unit level) -----------------------------------------------------


def mplan(name: str, plan_id: int = 1) -> PlanFacts:
    return PlanFacts(
        plan_id=plan_id, root=None, stage_root=mongo_plan.parse_plan(stored(name)), checked_at=NOW
    )


def mop(
    fid: int,
    query: str = '{"find": "orders", "filter": {"customerId": "?"}}',
    calls: int = 500,
    returned: int = 2_000,
    docs: int = 0,
    keys: int = 0,
    namespace: str = "shop.orders",
) -> OperationFacts:
    return OperationFacts(
        fid,
        query,
        calls,
        100.0,
        0.2,
        engine="mongodb",
        namespace=namespace,
        rows=returned,
        docs_examined=docs,
        keys_examined=keys,
    )


def ctx(operations: list[OperationFacts], plans: dict[int, PlanFacts]) -> RuleContext:
    return RuleContext(
        source_id=1,
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        operations=operations,
        plans=plans,
    )


def fired(context: RuleContext) -> list[tuple[str, str]]:
    return sorted((d.rule_id, d.target_key) for d in run_rules(context))


def test_m1_collection_scan_fires_with_an_index_command() -> None:
    # 500 lookups by customerId, each reading all 50,000 orders to return about 4.
    context = ctx([mop(1, docs=500 * 50_000)], {1: mplan("collscan_customer", 9)})
    [d] = run_rules(context)
    assert (d.rule_id, d.target_key, d.confidence) == ("MDB-COLLSCAN", "operation:1", "high")
    assert d.action_sql == (
        'db.getSiblingDB("shop").getCollection("orders").createIndex({ "customerId": 1 })'
    )
    assert d.relation == "shop.orders" and "COLLSCAN" in d.summary
    plan_link = next(e for e in d.evidence if e["kind"] == "plan")
    assert (plan_link["plan_id"], plan_link["node_path"]) == (9, "0")
    facts = {f["key"]: f["value"] for f in d.facts}
    assert facts["examined_per_returned"] == 12_500.0


def test_m1_stays_silent_when_little_is_examined_per_result() -> None:
    # A collection scan returning most of what it reads is the right plan.
    assert fired(ctx([mop(1, docs=2_000, returned=1_900)], {1: mplan("collscan_customer")})) == []
    assert fired(ctx([mop(1, calls=3, docs=3 * 50_000)], {1: mplan("collscan_customer")})) == []


def test_m2_in_memory_sort_fires_and_suggests_the_sort_order() -> None:
    context = ctx([mop(2, docs=500 * 50_000, returned=500 * 20)], {2: mplan("sort_no_index", 4)})
    [d] = run_rules(context)
    assert (d.rule_id, d.confidence) == ("MDB-BLOCKING-SORT", "high")
    assert d.action_sql == (
        'db.getSiblingDB("shop").getCollection("orders").createIndex({ "placedAt": -1 })'
    )
    assert next(e for e in d.evidence if e["kind"] == "plan")["node_path"] == "0"


def test_filtered_scan_under_a_sort_is_one_collscan_finding() -> None:
    context = ctx([mop(3, docs=500 * 50_000)], {3: mplan("collscan_and_sort")})
    assert fired(context) == [("MDB-COLLSCAN", "operation:3")]


def test_m3_unanchored_regex_fires_on_statistics_with_the_reason() -> None:
    query = '{"find": "products", "filter": {"sku": {"$regex": "?"}}}'
    context = ctx(
        [mop(5, query=query, keys=500 * 20_000, docs=500, returned=500, namespace="shop.products")],
        {5: mplan("regex_unanchored", 6)},
    )
    [d] = run_rules(context)
    assert (d.rule_id, d.confidence) == ("MDB-EXAMINED-RATIO", "high")
    assert "isn't anchored" in d.summary and "sku_1" in d.summary
    assert d.action_sql is None
    assert next(e for e in d.evidence if e["kind"] == "plan")["node_path"] == "0.0"


def test_m3_without_regex_or_whole_index_is_medium_confidence() -> None:
    context = ctx([mop(6, keys=500 * 5_000, returned=500)], {6: mplan("ixscan_equality")})
    [d] = run_rules(context)
    assert (d.rule_id, d.confidence) == ("MDB-EXAMINED-RATIO", "medium")


def test_mongo_rules_ignore_postgres_plans_and_vice_versa() -> None:
    # A MongoDB operation with no plan, and a PostgreSQL operation: nothing to judge.
    pg = OperationFacts(9, "SELECT 1", 1000, 500.0, 0.5)
    assert fired(ctx([mop(1, docs=10**8), pg], {})) == []


def test_mongo_demo_gives_exactly_one_finding_per_planted_problem() -> None:
    """M1-M3 plus the MongoDB controls (id lookup, indexed equality, sort on _id, anchored
    regex): three findings, one per planted problem."""
    regex = '{"find": "products", "filter": {"sku": {"$regex": "?"}}}'
    operations = [
        mop(1, docs=500 * 50_000, returned=2_000),  # M1
        mop(2, docs=500 * 50_000, returned=500 * 20),  # M2
        mop(3, query=regex, keys=500 * 20_000, docs=500, returned=500, namespace="shop.products"),
        mop(11, docs=500, keys=500, returned=500),  # id lookup
        mop(12, docs=500, keys=500, returned=500),  # indexed equality
        mop(13, docs=500 * 20, keys=500 * 20, returned=500 * 20),  # sort on _id
        mop(14, query=regex, docs=5_000, keys=5_010, returned=5_000),  # anchored regex
    ]
    plans = {
        1: mplan("collscan_customer", 1),
        2: mplan("sort_no_index", 2),
        3: mplan("regex_unanchored", 3),
        11: mplan("id_lookup", 11),
        12: mplan("ixscan_equality", 12),
        13: mplan("sort_by_id_index", 13),
        14: mplan("regex_anchored", 14),
    }
    assert fired(ctx(operations, plans)) == [
        ("MDB-BLOCKING-SORT", "operation:2"),
        ("MDB-COLLSCAN", "operation:1"),
        ("MDB-EXAMINED-RATIO", "operation:3"),
    ]


# --- profile grouping and the in-memory sample cache -------------------------------------------


def entry(command: dict[str, Any], millis: float = 2.0, ts: int = 0, **counts: int) -> ProfileEntry:
    return ProfileEntry(
        op="query",
        ns="shop.orders",
        millis=millis,
        nreturned=counts.get("returned", 1),
        docs_examined=counts.get("docs", 100),
        keys_examined=counts.get("keys", 0),
        ts=NOW + timedelta(seconds=ts),
        command=command,
    )


def test_group_profile_sums_per_shape_and_keeps_the_newest_command() -> None:
    entries = [
        entry({"find": "orders", "filter": {"customerId": 1}}, millis=3, ts=1),
        entry({"find": "orders", "filter": {"customerId": 2}}, millis=5, ts=2, docs=200),
        entry({"find": "orders", "filter": {"orderNo": 9}}, millis=1, ts=3, docs=1, keys=1),
        ProfileEntry("getmore", "shop.orders", 1, 1, 1, 1, NOW, {"getMore": 1}),
    ]
    totals = group_profile(entries)
    assert len(totals) == 2
    by_customer = next(t for t in totals.values() if "customerId" in t.shape.text)
    assert (by_customer.calls, by_customer.millis, by_customer.docs_examined) == (2, 8.0, 300)
    assert by_customer.last_command == {"find": "orders", "filter": {"customerId": 2}}


def test_profile_entries_never_show_their_command_in_repr() -> None:
    text = repr(entry({"find": "orders", "filter": {"email": "secret@example.com"}}))
    assert "secret@example.com" not in text


def test_sample_cache_is_bounded_and_keeps_the_newest() -> None:
    cache = SampleCache(size=2)
    shapes = []
    for n in range(3):
        command = {"find": "orders", "filter": {f"field{n}": f"secret-{n}"}}
        shape = shape_of("query", "shop.orders", command)
        assert shape is not None
        shapes.append(shape)
        cache.put(shape, command, NOW)
    assert len(cache) == 2
    assert cache.get(shapes[0].hash) is None  # least recently seen is dropped
    sample = cache.get(shapes[2].hash)
    assert sample is not None
    assert sample.command == {"find": "orders", "filter": {"field2": "secret-2"}}
    assert "secret" not in repr(sample)  # the values never reach a log line
    assert cache.get(None) is None
