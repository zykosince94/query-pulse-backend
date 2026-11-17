"""The demo's sample AI answers: valid against the real evidence, labelled, and only filling gaps.

The samples are written in advance, so the risk is that they drift from the evidence bundles
the rules produce. Each one is checked here against the saved bundle of its planted problem
(evals/bundles/, the same bundles the live stack builds) with the very checks a provider's
answer goes through.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from query_pulse.ai.answers import Kind, check
from query_pulse.ai.evidence import Bundle
from query_pulse.ai.models import AIAnswer
from query_pulse.config import Settings
from query_pulse.db import create_engine, create_sessionmaker, utcnow
from query_pulse.demo.ai_samples import SAMPLE_MODEL, SAMPLE_PROVIDER, SAMPLES, attach_samples
from query_pulse.findings import engine as findings_engine
from query_pulse.findings.models import Finding
from query_pulse.main import create_app
from tests.conftest import ADMIN_PASSWORD, DEMO_PASSWORD, make_settings, run_bootstrap
from tests.test_findings import seed_demo

Maker = async_sessionmaker[AsyncSession]
BUNDLES = Path(__file__).resolve().parent.parent / "evals" / "bundles"


def saved_bundles() -> dict[str, Bundle]:
    found: dict[str, Bundle] = {}
    for path in sorted(BUNDLES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        found[data["rule_id"]] = Bundle(data["bundle"])
    return found


# --- the samples themselves -------------------------------------------------------------------


def test_every_planted_problem_has_an_explanation_and_a_draft() -> None:
    assert set(SAMPLES) == set(saved_bundles())
    assert len(SAMPLES) == 7
    for rule, kinds in SAMPLES.items():
        assert set(kinds) == {"explain", "draft"}, rule


@pytest.mark.parametrize("rule", sorted(SAMPLES))
@pytest.mark.parametrize("kind", ["explain", "draft"])
def test_each_sample_passes_the_same_checks_as_a_provider_answer(rule: str, kind: Kind) -> None:
    checked = check(kind, SAMPLES[rule][kind], saved_bundles()[rule])
    assert checked.status == "ok", checked.error
    # Every claim is backed by evidence, and the summary too (the panel flags uncited claims).
    assert checked.summary is not None and checked.summary.cited
    assert all(claim.cited for claim in checked.claims)
    # The system prompt asks for at most five claims; the samples follow it.
    assert 1 <= len(checked.claims) <= 5


def test_drafts_suggest_the_same_fix_as_the_rule() -> None:
    expected = {
        "PG-SEQSCAN-HOT": "CREATE INDEX CONCURRENTLY ON public.orders (customer_id);",
        "PG-SORT-NO-INDEX": "CREATE INDEX CONCURRENTLY ON public.orders (created_at DESC);",
        "PG-UNUSED-INDEX": "DROP INDEX CONCURRENTLY public.idx_orders_legacy_lookup;",
        "MDB-COLLSCAN": 'createIndex({ "customerId": 1 })',
        "MDB-BLOCKING-SORT": 'createIndex({ "placedAt": -1 })',
        "PG-LEADING-WILDCARD": "gin (name gin_trgm_ops)",
    }
    for rule, fragment in expected.items():
        assert fragment in SAMPLES[rule]["draft"]["code"], rule
    languages = {rule: kinds["draft"]["language"] for rule, kinds in SAMPLES.items()}
    assert {r for r, lang in languages.items() if lang == "mongosh"} == {
        "MDB-COLLSCAN",
        "MDB-BLOCKING-SORT",
        "MDB-EXAMINED-RATIO",
    }


# --- attaching them to findings ---------------------------------------------------------------


@pytest.fixture
def maker(database_url: str) -> Iterator[Maker]:
    db = create_engine(database_url)
    yield create_sessionmaker(db)
    asyncio.run(db.dispose())


@pytest.fixture
def settings(database_url: str) -> Settings:
    settings = make_settings(database_url)
    run_bootstrap(settings)
    return settings


def raise_findings(maker: Maker, settings: Settings) -> dict[str, int]:
    """The PostgreSQL planted problems, raised by the real rules on the Phase 5 demo evidence."""
    now = datetime.now(UTC)
    ids = seed_demo(maker, now)

    async def go() -> dict[str, int]:
        async with maker() as session:
            await findings_engine.evaluate_source(session, ids["source"], settings, now)
            await session.commit()
            return {f.rule_id: f.id for f in await session.scalars(select(Finding))}

    return asyncio.run(go())


def attach(maker: Maker) -> int:
    async def go() -> int:
        async with maker() as session:
            return await attach_samples(session)

    return asyncio.run(go())


def answers(maker: Maker) -> list[AIAnswer]:
    async def go() -> list[AIAnswer]:
        async with maker() as session:
            return list(await session.scalars(select(AIAnswer).order_by(AIAnswer.id)))

    return asyncio.run(go())


def test_samples_attach_to_the_planted_findings_once(maker: Maker, settings: Settings) -> None:
    found = raise_findings(maker, settings)
    planted = {rule for rule in found if rule in SAMPLES}
    assert planted == {
        "PG-SEQSCAN-HOT",
        "PG-SORT-NO-INDEX",
        "PG-LEADING-WILDCARD",
        "PG-UNUSED-INDEX",
    }

    assert attach(maker) == 2 * len(planted)
    stored = answers(maker)
    assert {(a.finding_id, a.kind) for a in stored} == {
        (found[rule], kind) for rule in planted for kind in ("explain", "draft")
    }
    assert {(a.provider, a.model) for a in stored} == {(SAMPLE_PROVIDER, SAMPLE_MODEL)}
    # Every 30 seconds the job runs again: nothing new while the evidence is the same.
    assert attach(maker) == 0
    assert len(answers(maker)) == len(stored)


def test_a_real_answer_is_never_replaced(maker: Maker, settings: Settings) -> None:
    found = raise_findings(maker, settings)
    attach(maker)
    real_id = found["PG-SEQSCAN-HOT"]

    async def ask_real() -> None:
        async with maker() as session:
            sample = await session.scalar(
                select(AIAnswer).where(AIAnswer.finding_id == real_id, AIAnswer.kind == "explain")
            )
            assert sample is not None
            session.add(
                AIAnswer(
                    finding_id=real_id,
                    kind="explain",
                    evidence_hash=sample.evidence_hash,
                    provider="openai",
                    model="a-real-model",
                    answer=sample.answer,
                    created_at=utcnow(),
                )
            )
            await session.commit()

    asyncio.run(ask_real())
    assert attach(maker) == 0
    latest = [a for a in answers(maker) if a.finding_id == real_id and a.kind == "explain"][-1]
    assert latest.provider == "openai"


def test_new_evidence_repoints_the_sample_instead_of_piling_up_rows(
    maker: Maker, settings: Settings
) -> None:
    found = raise_findings(maker, settings)
    attach(maker)
    before = answers(maker)

    async def stale_everything() -> None:
        async with maker() as session:
            for row in await session.scalars(select(AIAnswer)):
                row.evidence_hash = "0" * 64  # as if the evidence had changed since
            await session.commit()

    asyncio.run(stale_everything())
    assert attach(maker) == 2 * len([r for r in found if r in SAMPLES])
    after = answers(maker)
    # Same rows, now current again: the live numbers drift all day, the table must not grow.
    assert [a.id for a in after] == [a.id for a in before]
    assert {a.evidence_hash for a in after} == {a.evidence_hash for a in before}
    assert attach(maker) == 0


def test_the_api_shows_samples_to_everyone_with_ai_off(settings: Settings) -> None:
    found = {}
    db = create_engine(settings.database_url)
    maker = create_sessionmaker(db)
    try:
        found = raise_findings(maker, settings)
        attach(maker)
    finally:
        asyncio.run(db.dispose())

    for user, password in (("admin", ADMIN_PASSWORD), ("demo", DEMO_PASSWORD)):
        with TestClient(create_app(settings)) as client:
            assert client.post(
                "/api/auth/login", json={"username": user, "password": password}
            ).is_success
            body = client.get(f"/api/findings/{found['PG-SEQSCAN-HOT']}/ai").json()
            assert body["enabled"] is False
            for kind in ("explain", "draft"):
                assert body[kind]["provider"] == SAMPLE_PROVIDER
                assert body[kind]["current"] is True
                assert body[kind]["status"] == "ok"
            assert (
                body["draft"]["code"] == "CREATE INDEX CONCURRENTLY ON public.orders (customer_id);"
            )


def test_nothing_is_attached_without_planted_findings(maker: Maker, settings: Settings) -> None:
    async def count() -> int:
        async with maker() as session:
            return int(await session.scalar(select(func.count()).select_from(AIAnswer)) or 0)

    assert attach(maker) == 0
    assert asyncio.run(count()) == 0
