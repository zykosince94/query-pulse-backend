"""The AI assistant (Phase 7) with the fake provider and a mocked OpenAI: no network, no cost.

- QP-AC-26 (API half): with AI off, asking is refused and no provider request is made.
- QP-AC-27: an answer citing an evidence id that wasn't in the bundle is rejected.
- QP-AC-28: bundles for all seven planted problems hold no literals, credentials or
  connection strings.
Plus: the key is never returned or logged, answers are cached by evidence, destructive drafts
are rejected, and the OpenAI provider's request and error handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.ai.answers import SCHEMAS, SYSTEM, Kind, check, forbidden_actions, prompt, score
from query_pulse.ai.evidence import (
    Bundle,
    assemble,
    mongo_plan_items,
    postgres_plan_items,
    redact_sql,
    redact_text,
)
from query_pulse.ai.provider import FakeProvider, OpenAIProvider, ProviderError
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.findings import engine as findings_engine
from query_pulse.findings.models import Finding
from query_pulse.findings.rules import Detection, PlanFacts, run_rules
from query_pulse.main import create_app
from query_pulse.sources.models import DataSource
from tests.conftest import ADMIN_PASSWORD, DEMO_PASSWORD, make_settings, run_bootstrap
from tests.test_findings import context, index, like_plan, op, plan, seed_demo
from tests.test_mongodb import ctx as mongo_ctx
from tests.test_mongodb import mop, mplan

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
Maker = async_sessionmaker[AsyncSession]
MARKER_KEY = "sk-marker-ai-key-never-shown-7Q2x"
MARKER_PASSWORD = "marker-source-password-91XZ"
SECRET_HOST = "db-secret-host.example"
SECRET_USER = "qp_secret_user"

# --- redaction --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("(customer_id = 42)", "(customer_id = ?)"),
        ("((name)::text ~~ '%lamp%'::text)", "((name)::text ~~ '?'::text)"),
        ("(email = 'o''brien@x.io')", "(email = '?')"),
        ("(customer_id = $1)", "(customer_id = $1)"),
        ("orders2.total > 10.5", "orders2.total > ?"),
        ("total < -3", "total < ?"),
    ],
)
def test_sql_literals_are_redacted_and_parameters_kept(sql: str, expected: str) -> None:
    assert redact_sql(sql) == expected


def test_prose_keeps_statistics_but_loses_quoted_literals() -> None:
    assert (
        redact_text("reads all 200,000 rows of x ('%abc%')") == "reads all 200,000 rows of x ('?')"
    )


# --- QP-AC-28: the seven bundles --------------------------------------------------------------


def as_finding(d: Detection) -> Finding:
    return Finding(
        id=1,
        source_id=1,
        rule_id=d.rule_id,
        target_kind=d.target_kind,
        target_key=d.target_key,
        fingerprint_id=d.fingerprint_id,
        relation=d.relation,
        title=d.title,
        summary=d.summary,
        confidence=d.confidence,
        evidence_strength="plan",
        suggested_action=d.suggested_action,
        action_sql=d.action_sql,
        facts=d.facts,
        evidence=d.evidence,
    )


LITERALS = ["424242", "leak@example.com", "9876543", "secret-term", "SKU-0123"]


def seven_bundles() -> dict[str, Bundle]:
    """A bundle per planted problem, from real plans with literals planted in them."""
    p1_plan = plan("p1_orders_by_customer", 1)
    assert p1_plan.root is not None
    p1_root = p1_plan.root.model_copy(update={"filter": "(customer_id = 424242)"})
    pg = context(
        operations=[op(1, share=0.5), op(2, share=0.4), op(3, share=0.02)],
        plans={
            1: PlanFacts(1, p1_root, NOW),
            2: plan("p2_latest_orders", 2),
            3: like_plan("'%secret-term%'"),
        },
        indexes=[index("idx_orders_legacy_lookup")],
    )
    regex = '{"find": "products", "filter": {"sku": {"$regex": "?"}}}'
    mongo = mongo_ctx(
        [
            mop(21, docs=500 * 50_000, returned=2_000),
            mop(22, docs=500 * 50_000, returned=10_000),
            mop(23, query=regex, keys=500 * 20_000, docs=500, returned=500),
        ],
        {
            21: mplan("collscan_customer", 21),
            22: mplan("sort_no_index", 22),
            23: mplan("regex_unanchored", 23),
        },
    )
    names = {
        "PG-SEQSCAN-HOT": "P1",
        "PG-SORT-NO-INDEX": "P2",
        "PG-LEADING-WILDCARD": "P3",
        "PG-UNUSED-INDEX": "P4",
        "MDB-COLLSCAN": "M1",
        "MDB-BLOCKING-SORT": "M2",
        "MDB-EXAMINED-RATIO": "M3",
    }
    bundles: dict[str, Bundle] = {}
    for context_, engine in ((pg, "postgres"), (mongo, "mongodb")):
        for detection in run_rules(context_):
            plan_facts = context_.plans.get(detection.fingerprint_id or 0)
            items: list[dict[str, Any]] = []
            if plan_facts is not None and plan_facts.root is not None:
                items = postgres_plan_items(plan_facts.root)
            elif plan_facts is not None and plan_facts.stage_root is not None:
                items = mongo_plan_items(plan_facts.stage_root)
            text = (
                "SELECT * FROM orders WHERE email = 'leak@example.com' AND id = 9876543"
                if engine == "postgres"
                else regex
            )
            bundles[names[detection.rule_id]] = assemble(
                finding=as_finding(detection),
                engine=engine,
                server_version="16.4" if engine == "postgres" else "7.0.14",
                operation={"text": redact_sql(text) if engine == "postgres" else text},
                plan_items=items,
                relations=[],
            )
    return bundles


def test_there_is_a_bundle_for_every_planted_problem() -> None:
    assert sorted(seven_bundles()) == ["M1", "M2", "M3", "P1", "P2", "P3", "P4"]


@pytest.mark.parametrize("problem", ["P1", "P2", "P3", "P4", "M1", "M2", "M3"])
def test_bundles_hold_no_literals(problem: str) -> None:
    bundle = seven_bundles()[problem]
    for literal in LITERALS:
        assert literal not in bundle.text, (problem, literal)
    ids = bundle.ids
    assert {"rule", "finding", "source", "operation"} <= ids
    assert any(i.startswith("fact:") for i in ids)
    if problem != "P4":  # the unused index has no plan
        assert any(i.startswith("plan:") for i in ids)


def test_bundle_hash_is_stable_and_changes_with_the_evidence() -> None:
    first, again = seven_bundles()["P1"], seven_bundles()["P1"]
    assert first.hash == again.hash
    assert first.hash != seven_bundles()["P2"].hash


def test_the_hash_ignores_drift_but_not_material_change() -> None:
    """Counts drift every rule run; an answer stays current until the evidence really moves."""
    base = seven_bundles()["P1"].document

    def with_calls(calls: int, sampled_at: str = "2026-11-16T10:00:00") -> Bundle:
        document = json.loads(json.dumps(base))
        for item in document["items"]:
            if item["id"] == "fact:calls":
                item["data"]["value"] = calls
        document["items"].append(
            {"id": "table:public.orders", "kind": "table", "data": {"sampled_at": sampled_at}}
        )
        return Bundle(document)

    assert with_calls(1000).hash == with_calls(1040, "2026-11-16T10:05:00").hash  # drift
    assert with_calls(1000).hash != with_calls(10_000).hash  # ten times more: a new question


# --- answers: citations, uncited claims, forbidden drafts --------------------------------------


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def ask_fake(mode: str, kind: Kind, bundle: Bundle) -> dict[str, Any]:

    name, schema = SCHEMAS[kind]
    raw = run(
        FakeProvider(mode).complete(
            model="m",
            system=SYSTEM,
            user=prompt(kind, bundle),
            schema_name=name,
            schema=schema,
        )
    )
    return raw


def test_an_answer_citing_unknown_evidence_is_rejected() -> None:
    """QP-AC-27."""
    bundle = seven_bundles()["P1"]
    checked = check("explain", ask_fake("invalid_citation", "explain", bundle), bundle)
    assert checked.status == "rejected"
    assert checked.unknown_citations == ["plan:9.9.9"]
    assert "plan:9.9.9" in (checked.error or "") and checked.claims == []


def test_uncited_claims_are_kept_but_flagged() -> None:
    bundle = seven_bundles()["M1"]
    checked = check("explain", ask_fake("uncited", "explain", bundle), bundle)
    assert checked.status == "ok"
    assert [c.cited for c in checked.claims] == [True, True, False]


def test_a_destructive_draft_is_rejected() -> None:
    bundle = seven_bundles()["P1"]
    checked = check("draft", ask_fake("forbidden", "draft", bundle), bundle)
    assert checked.status == "rejected" and checked.forbidden


def test_a_malformed_answer_is_rejected() -> None:
    checked = check("explain", {"summary": "not a claim"}, seven_bundles()["P1"])
    assert checked.status == "rejected" and "shape" in (checked.error or "")


@pytest.mark.parametrize(
    ("code", "bad"),
    [
        ("CREATE INDEX CONCURRENTLY ON public.orders (customer_id);", False),
        ("DROP INDEX CONCURRENTLY public.idx_orders_legacy_lookup;", False),
        ('db.getSiblingDB("shop").getCollection("orders").createIndex({ "a": 1 })', False),
        ("DROP TABLE orders;", True),
        ("TRUNCATE orders;", True),
        ("DELETE FROM orders WHERE true;", True),
        ("UPDATE orders SET total = 0;", True),
        ("ALTER SYSTEM SET work_mem = '1GB';", True),
        ("GRANT ALL ON orders TO public;", True),
        ('db.getCollection("orders").drop()', True),
        ('db.getCollection("orders").deleteMany({})', True),
        ("db.dropDatabase()", True),
    ],
)
def test_forbidden_actions(code: str, bad: bool) -> None:
    assert bool(forbidden_actions(code)) is bad


def test_score_rewards_the_right_cause_and_valid_citations() -> None:
    bundle = seven_bundles()["P1"]
    good = check("explain", ask_fake("valid", "explain", bundle), bundle)
    assert score(good, [["fired"]]) == {"cause": 1.0, "citations": 1.0, "safe": 1.0}
    assert score(good, [["fired"], ["no such words"]])["cause"] == 0.5
    rejected = check("explain", ask_fake("invalid_citation", "explain", bundle), bundle)
    assert score(rejected, [["fired"]]) == {"cause": 0.0, "citations": 0.0, "safe": 1.0}


# --- OpenAI over a mocked transport -----------------------------------------------------------


def openai(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAIProvider:
    return OpenAIProvider("sk-test-key-abcd", transport=httpx.MockTransport(handler), timeout=5)


def test_openai_request_uses_a_strict_schema_and_parses_the_answer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        content = json.dumps({"summary": {"text": "x", "evidence": ["rule"]}})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result: dict[str, Any] = run(
        openai(handler).complete(
            model="m-1",
            system="sys",
            user="usr",
            schema_name="explanation",
            schema={"type": "object"},
        )
    )
    assert result == {"summary": {"text": "x", "evidence": ["rule"]}}
    [request] = seen
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test-key-abcd"
    body = json.loads(request.content)
    assert body["model"] == "m-1"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (
            401,
            {"message": "Incorrect API key provided: sk-test-****abcd", "code": "invalid_api_key"},
            "rejected the API key",
        ),
        (
            404,
            {"message": "model not found", "code": "model_not_found"},
            "doesn't know model 'm-1'",
        ),
        (429, {"message": "quota", "code": "insufficient_quota"}, "no quota left"),
        (429, {"message": "slow down", "code": "rate_limit_exceeded"}, "rate-limiting"),
        (503, {"message": "down"}, "server error"),
    ],
)
def test_openai_errors_become_messages_without_the_key(
    status: int, error: dict[str, str], message: str
) -> None:
    provider = openai(lambda _: httpx.Response(status, json={"error": error}))
    with pytest.raises(ProviderError) as raised:
        run(provider.check("m-1"))
    assert message in str(raised.value)
    assert "sk-test" not in str(raised.value) and "abcd" not in str(raised.value)
    assert "abcd" not in repr(provider)


def test_openai_timeout_and_refusal() -> None:
    def slow(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderError, match="didn't answer"):
        run(openai(slow).check("m"))
    refusal = openai(
        lambda _: httpx.Response(
            200, json={"choices": [{"message": {"content": None, "refusal": "no"}}]}
        )
    )
    with pytest.raises(ProviderError, match="declined"):
        run(refusal.complete(model="m", system="s", user="u", schema_name="n", schema={}))


# --- the API, end to end with the fake provider ------------------------------------------------


@pytest.fixture
def ai_settings(database_url: str) -> Settings:
    settings = make_settings(database_url, ai_allow_fake_provider=True)
    run_bootstrap(settings)
    return settings


@pytest.fixture
def api(ai_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(ai_settings)) as client:
        yield client


@pytest.fixture
def ai_maker(database_url: str) -> Iterator[Maker]:
    db = create_engine(database_url)
    yield create_sessionmaker(db)
    asyncio.run(db.dispose())


def login(client: TestClient, user: str = "admin") -> TestClient:
    password = ADMIN_PASSWORD if user == "admin" else DEMO_PASSWORD
    assert (
        client.post("/api/auth/login", json={"username": user, "password": password}).status_code
        == 200
    )
    return client


def seeded_findings(maker: Maker, settings: Settings) -> dict[str, int]:
    """The Phase 5 demo evidence, on a source with a marker password, host and username."""
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)
    cipher = CredentialCipher(settings.fernet_keys)

    async def _go() -> dict[str, int]:
        async with maker() as session:
            source = await session.get(DataSource, ids["source"])
            assert source is not None
            source.host = SECRET_HOST
            source.username = SECRET_USER
            source.password_encrypted = cipher.encrypt(MARKER_PASSWORD)
            await findings_engine.evaluate_source(session, ids["source"], settings, now)
            await session.commit()
            return {f.rule_id: f.id for f in await session.scalars(select(Finding))}

    return run(_go())


def enable(client: TestClient, **overrides: Any) -> httpx.Response:
    body = {"provider": "fake", "model": "fake-model", "api_key": MARKER_KEY, "enabled": True}
    body.update(overrides)
    response: httpx.Response = client.put("/api/ai/settings", json=body)
    return response


def test_ai_is_off_by_default_and_makes_no_provider_call(
    api: TestClient, ai_maker: Maker, ai_settings: Settings
) -> None:
    """QP-AC-26 (API half)."""
    found = seeded_findings(ai_maker, ai_settings)
    client = login(api)
    settings = client.get("/api/ai/settings").json()
    assert (settings["enabled"], settings["key_set"], settings["provider_calls"]) == (
        False,
        False,
        0,
    )
    assert {p["id"] for p in settings["providers"]} == {"openai", "fake"}
    response = client.post(f"/api/findings/{found['PG-SEQSCAN-HOT']}/ai/explain")
    assert response.status_code == 409 and "AI is off" in response.json()["detail"]
    assert client.get(f"/api/findings/{found['PG-SEQSCAN-HOT']}/ai").json()["enabled"] is False
    assert client.get("/api/ai/settings").json()["provider_calls"] == 0


def test_the_bundle_never_holds_credentials_or_where_the_server_is(
    api: TestClient, ai_maker: Maker, ai_settings: Settings
) -> None:
    """QP-AC-28 on a real finding: the source's password, host and username aren't sent."""
    found = seeded_findings(ai_maker, ai_settings)
    client = login(api, "demo")  # anyone can see what would be sent
    for finding_id in found.values():
        body = client.get(f"/api/findings/{finding_id}/ai/bundle").json()
        text = json.dumps(body)
        for secret in (MARKER_PASSWORD, SECRET_HOST, SECRET_USER, "postgresql://", "mongodb://"):
            assert secret not in text, secret
        assert body["evidence_hash"] and body["items"][0]["id"] == "rule"
    p1 = client.get(f"/api/findings/{found['PG-SEQSCAN-HOT']}/ai/bundle").json()
    kinds = {item["kind"] for item in p1["items"]}
    assert {"rule", "finding", "source", "operation", "fact", "plan_node", "table"} <= kinds


def test_turn_on_explain_draft_and_cache(
    api: TestClient, ai_maker: Maker, ai_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    found = seeded_findings(ai_maker, ai_settings)
    client = login(api)
    turned_on = enable(client)
    assert turned_on.status_code == 200, turned_on.text
    body = turned_on.json()
    assert (body["enabled"], body["key_hint"], body["provider_calls"]) == (True, "…7Q2x", 1)

    p1 = found["PG-SEQSCAN-HOT"]
    first = client.post(f"/api/findings/{p1}/ai/explain").json()
    assert (first["status"], first["cached"], first["current"]) == ("ok", False, True)
    assert first["summary"]["cited"] and all(c["cited"] for c in first["claims"])
    again = client.post(f"/api/findings/{p1}/ai/explain").json()
    assert again["cached"] is True and again["claims"] == first["claims"]
    assert client.get("/api/ai/settings").json()["provider_calls"] == 2  # check + one answer

    draft = client.post(f"/api/findings/{p1}/ai/draft").json()
    assert draft["language"] == "sql" and "CREATE INDEX CONCURRENTLY" in draft["code"]
    state = login(TestClient(api.app), "demo").get(f"/api/findings/{p1}/ai").json()
    assert state["enabled"] and state["explain"]["status"] == "ok" and state["draft"]["code"]

    # The key never comes back, and never reaches a log line.
    for path in ("/api/ai/settings", f"/api/findings/{p1}/ai"):
        assert MARKER_KEY not in client.get(path).text
    assert MARKER_KEY not in turned_on.text and MARKER_KEY not in caplog.text


def test_invalid_citations_and_destructive_drafts_are_rejected_not_cached(
    api: TestClient, ai_maker: Maker, ai_settings: Settings
) -> None:
    """QP-AC-27 through the API."""
    found = seeded_findings(ai_maker, ai_settings)
    client = login(api)
    enable(client)
    p2 = found["PG-SORT-NO-INDEX"]
    api.app.state.ai_provider_factory = lambda name, key: FakeProvider("invalid_citation")  # type: ignore[attr-defined]
    rejected = client.post(f"/api/findings/{p2}/ai/explain").json()
    assert rejected["status"] == "rejected" and "plan:9.9.9" in rejected["error"]
    assert rejected["claims"] == []
    assert client.get(f"/api/findings/{p2}/ai").json()["explain"] is None  # not cached

    api.app.state.ai_provider_factory = lambda name, key: FakeProvider("forbidden")  # type: ignore[attr-defined]
    draft = client.post(f"/api/findings/{p2}/ai/draft").json()
    assert draft["status"] == "rejected" and "never suggests" in draft["error"]


def test_provider_failures_are_502_with_a_message(
    api: TestClient, ai_maker: Maker, ai_settings: Settings
) -> None:
    found = seeded_findings(ai_maker, ai_settings)
    client = login(api)
    enable(client)

    class Broken(FakeProvider):
        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            raise ProviderError("OpenAI is rate-limiting this key. Try again in a minute.")

    api.app.state.ai_provider_factory = lambda name, key: Broken()  # type: ignore[attr-defined]
    response = client.post(f"/api/findings/{found['PG-SEQSCAN-HOT']}/ai/explain")
    assert response.status_code == 502 and "rate-limiting" in response.json()["detail"]


def test_settings_rules_bad_keys_retargeting_and_deleting(
    api: TestClient, ai_maker: Maker, ai_settings: Settings
) -> None:
    client = login(api)
    assert enable(client, api_key=None).status_code == 400  # nothing saved yet to turn on with
    assert client.get("/api/ai/settings").json()["enabled"] is False

    # A key OpenAI refuses: nothing is saved.
    api.app.state.ai_provider_factory = lambda name, key: openai(  # type: ignore[attr-defined]
        lambda _: httpx.Response(401, json={"error": {"code": "invalid_api_key"}})
    )
    refused = enable(client, provider="openai", model="some-model")
    assert refused.status_code == 400 and "rejected the API key" in refused.json()["detail"]
    assert client.get("/api/ai/settings").json()["key_set"] is False

    api.app.state.ai_provider_factory = lambda name, key: FakeProvider()  # type: ignore[attr-defined]
    assert enable(client).status_code == 200
    # A saved key is only sent to the provider it was saved for.
    retarget = enable(client, provider="openai", api_key=None)
    assert retarget.status_code == 400 and "Enter the API key again" in retarget.json()["detail"]
    # Turning off keeps the key; deleting it turns AI off.
    off = enable(client, api_key=None, enabled=False).json()
    assert (off["enabled"], off["key_set"]) == (False, True)
    deleted = client.delete("/api/ai/settings/key").json()
    assert (deleted["enabled"], deleted["key_set"], deleted["key_hint"]) == (False, False, None)
    assert enable(client, provider="nonsense").status_code == 400
    assert enable(client, model="has spaces").status_code == 422


def test_the_fake_provider_is_never_offered_unless_allowed(login_client: TestClient) -> None:
    providers = login_client.get("/api/ai/settings").json()["providers"]
    assert [p["id"] for p in providers] == ["openai"]
    assert (
        login_client.put(
            "/api/ai/settings", json={"provider": "fake", "model": "m", "enabled": False}
        ).status_code
        == 400
    )


@pytest.fixture
def login_client(login: Callable[[str], TestClient]) -> TestClient:
    return login("admin")


def test_runtime_settings_are_read_only_values_from_the_environment(
    login_client: TestClient,
) -> None:
    body = login_client.get("/api/settings").json()
    assert body == {
        "retention_days": 14,
        "collector_interval_seconds": 60.0,
        "rules_interval_seconds": 60.0,
        "rules_window_minutes": 60,
    }


def test_answers_go_stale_when_the_evidence_changes(
    api: TestClient, ai_maker: Maker, ai_settings: Settings
) -> None:
    found = seeded_findings(ai_maker, ai_settings)
    client = login(api)
    enable(client)
    p1 = found["PG-SEQSCAN-HOT"]
    client.post(f"/api/findings/{p1}/ai/explain")

    async def change() -> None:
        async with ai_maker() as session:
            finding = await session.get(Finding, p1)
            assert finding is not None
            finding.facts = [*finding.facts, {"key": "extra", "label": "Extra", "value": 1}]
            await session.commit()

    run(change())
    state = client.get(f"/api/findings/{p1}/ai").json()
    assert state["explain"]["current"] is False
    assert client.get("/api/findings/9999/ai").status_code == 404


# --- QP-AC-29: the evaluation harness ------------------------------------------------------------


def test_the_eval_runs_over_all_seven_saved_bundles() -> None:
    """`make ai-eval` with the fake provider: every bundle is asked both tasks, every answer
    passes the checks. A real run (with a key) is manual and its scores go in the README."""
    from scripts.ai_eval import evaluate, load_cases, report, summary

    cases = load_cases()
    assert sorted(str(c["problem"]) for c in cases) == ["M1", "M2", "M3", "P1", "P2", "P3", "P4"]
    for case in cases:  # the saved set is safe to publish
        text = json.dumps(case)
        for secret in ("demo-postgres", "querypulse_monitor", "mongo:27017", "postgresql://"):
            assert secret not in text
    rows = run(evaluate(FakeProvider(), "fake", cases))
    assert len(rows) == 14 and all(r.status == "ok" for r in rows)
    totals = summary(rows)
    assert totals["citations"] == 1.0 and totals["safe"] == 1.0
    assert "| P1 | explain | ok |" in report(rows, "fake", "fake")
