"""Acceptance: the full stack under the demo workload.

QP-AC-14 (P1 ranks first), QP-AC-15 end to end (P1's plan), QP-AC-18 (P1-P4 each produce
exactly one open finding from their rule), QP-AC-19 (controls produce none) and QP-AC-20
(every evidence link resolves).

Runs over HTTP against a running stack (`make demo`), so it proves what a user would see:

    QP_ACCEPTANCE_URL=http://127.0.0.1:8000 uv run pytest tests/acceptance

Skipped when QP_ACCEPTANCE_URL isn't set. The test registers the demo PostgreSQL as a source
if it isn't already, then waits until the collector has seen every demo statement.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("QP_ACCEPTANCE_URL")
pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(not BASE_URL, reason="QP_ACCEPTANCE_URL isn't set (needs `make demo`)"),
]

ADMIN_PASSWORD = os.environ.get("QP_ACCEPTANCE_ADMIN_PASSWORD", "admin-demo-password")
# How long to wait for the collector to see the whole workload (cycles are 15 s in the demo).
WAIT_SECONDS = float(os.environ.get("QP_ACCEPTANCE_WAIT_SECONDS", "360"))

# How the demo source is reached from inside the stack (the app container), not from here.
DEMO_SOURCE = {
    "name": "Demo shop (PostgreSQL)",
    "engine": "postgres",
    "host": "demo-postgres",
    "port": 5432,
    "database": "shop",
    "username": "querypulse_monitor",
    "password": "monitor",
    "tls_mode": "disable",
}
P1 = "FROM orders WHERE customer_id = $1"
# Every statement the demo workload runs (P1-P3 and controls C1-C4).
DEMO_STATEMENTS = [
    P1,
    "ORDER BY created_at DESC LIMIT",
    "WHERE name LIKE $1",
    "FROM customers WHERE id = $1",
    "FROM orders WHERE id = $1",
    "WHERE category = $1",
    "WHERE id BETWEEN $1 AND $1 + $2",
]


@pytest.fixture(scope="module")
def api() -> Iterator[httpx.Client]:
    # No keep-alive: uvicorn drops idle connections after 5 s, the same as our poll interval,
    # and reusing a connection the server just closed fails with "Server disconnected".
    limits = httpx.Limits(max_keepalive_connections=0)
    with httpx.Client(base_url=str(BASE_URL), timeout=30, limits=limits) as client:
        response = client.post(
            "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200, response.text
        yield client


@pytest.fixture(scope="module")
def source_id(api: httpx.Client) -> int:
    for source in api.get("/api/sources").json():
        if source["host"] == DEMO_SOURCE["host"] and source["database"] == "shop":
            return int(source["id"])
    response = api.post("/api/sources", json=DEMO_SOURCE)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def wait_for(check: Any, what: str) -> Any:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {WAIT_SECONDS:g}s waiting for {what}")
        time.sleep(5)


def ranked(api: httpx.Client, source_id: int, sort: str = "total_time") -> list[dict[str, Any]]:
    response = api.get(
        "/api/operations",
        params={"minutes": 15, "sort": sort, "source_id": source_id, "limit": 100},
    )
    assert response.status_code == 200, response.text
    operations: list[dict[str, Any]] = response.json()["operations"]
    return operations


def demo_statements_seen(operations: list[dict[str, Any]]) -> bool:
    return all(any(s in op["query"] for op in operations) for s in DEMO_STATEMENTS)


def test_p1_ranks_first_by_total_time(api: httpx.Client, source_id: int) -> None:
    """QP-AC-14: under the demo workload, P1 ranks first by total time over 15 minutes."""
    operations = wait_for(
        lambda: (ops := ranked(api, source_id)) and demo_statements_seen(ops) and ops,
        "the collector to observe every demo statement",
    )
    top = operations[0]
    assert P1 in top["query"], [op["query"] for op in operations[:3]]
    assert top["share_of_total_time"] > 0.3


def test_p1_plan_shows_the_sequential_scan(api: httpx.Client, source_id: int) -> None:
    """QP-AC-15 on the running stack: the collector captured P1's plan with its Seq Scan."""
    p1 = wait_for(
        lambda: next((op for op in ranked(api, source_id) if P1 in op["query"]), None),
        "P1 to be observed",
    )
    plan = wait_for(
        lambda: (
            (body := api.get(f"/api/operations/{p1['id']}/plan").json())["status"] == "captured"
            and body
        ),
        "P1's plan to be captured",
    )
    nodes, stack = [], [plan["root"]]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(node["children"])
    assert any("Seq Scan" in n["node_type"] and n["relation_name"] == "orders" for n in nodes), [
        n["node_type"] for n in nodes
    ]


# The finding each planted problem must produce: (rule, what its target looks like).
EXPECTED_FINDINGS = {
    "P1": ("PG-SEQSCAN-HOT", P1),
    "P2": ("PG-SORT-NO-INDEX", "ORDER BY created_at DESC LIMIT"),
    "P3": ("PG-LEADING-WILDCARD", "WHERE name LIKE $1"),
    "P4": ("PG-UNUSED-INDEX", "public.idx_orders_legacy_lookup"),
}


def open_findings(api: httpx.Client, source_id: int) -> list[dict[str, Any]]:
    response = api.get("/api/findings", params={"source_id": source_id, "status": "open"})
    assert response.status_code == 200, response.text
    findings: list[dict[str, Any]] = response.json()["findings"]
    return findings


def matches(finding: dict[str, Any], rule: str, target: str) -> bool:
    return finding["rule_id"] == rule and (
        target in (finding["query"] or "") or target == finding["relation"]
    )


def all_planted_found(findings: list[dict[str, Any]]) -> bool:
    return all(
        any(matches(f, *expected) for f in findings) for expected in EXPECTED_FINDINGS.values()
    )


def test_each_planted_problem_produces_exactly_one_finding_and_controls_none(
    api: httpx.Client, source_id: int
) -> None:
    """QP-AC-18 and QP-AC-19 on the running stack."""
    findings = wait_for(
        lambda: (found := open_findings(api, source_id)) and all_planted_found(found) and found,
        "P1-P4 to produce their findings (P4 needs two minutes of index samples)",
    )
    for problem, (rule, target) in EXPECTED_FINDINGS.items():
        hits = [f for f in findings if matches(f, rule, target)]
        assert len(hits) == 1, (problem, hits)
    # Nothing else: the healthy controls (and everything else the demo runs) stay silent.
    assert len(findings) == len(EXPECTED_FINDINGS), [
        (f["rule_id"], f["query"] or f["relation"]) for f in findings
    ]


def test_every_finding_evidence_link_resolves(api: httpx.Client, source_id: int) -> None:
    """QP-AC-20: each evidence link points at an existing observation window, plan or sample."""
    findings = wait_for(lambda: open_findings(api, source_id), "findings to exist")
    for finding in findings:
        assert finding["evidence"], finding["rule_id"]
        for item in finding["evidence"]:
            assert item["available"], (finding["rule_id"], item)
            if item["kind"] == "plan":
                cited = api.get(f"/api/plans/{item['plan_id']}").json()
                assert cited["status"] == "captured" and cited["plan_id"] == item["plan_id"]
