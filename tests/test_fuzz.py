"""Schemathesis: fuzz every API operation from openapi.json (Phase 7 hardening).

Generated requests (valid and invalid) go to every endpoint as the signed-in admin, and every
response must be a non-5xx that matches the documented schema. Connections are faked (sources
connect to nothing, the AI provider is the fake one), so fuzzing never leaves the process.

QP_FUZZ_EXAMPLES sets the examples per operation (default 20; the nightly run uses more).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
import schemathesis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import response_schema_conformance

from query_pulse.ai.provider import FakeProvider
from query_pulse.config import Settings
from query_pulse.main import create_app
from query_pulse.sources import service
from tests.conftest import ADMIN_PASSWORD
from tests.test_sources import FakeAdapter

EXAMPLES = int(os.environ.get("QP_FUZZ_EXAMPLES", "20"))


@pytest.fixture
def app(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(service, "adapter_for", lambda engine: FakeAdapter())
    application = create_app(settings)
    application.state.ai_provider_factory = lambda name, key: FakeProvider()
    # Fuzzing sign-in with random passwords must not lock the test client out.
    application.state.login_limiter.max_failures = 10**9
    yield application


@pytest.fixture
def cookie(app: FastAPI) -> str:
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200
        return f"qp_session={client.cookies['qp_session']}"


@pytest.fixture
def api_schema(app: FastAPI) -> Any:
    return schemathesis.openapi.from_asgi("/openapi.json", app).exclude(
        # Signing out would end the session the rest of the run uses.
        operation_id=["logout"]
    )


schema = schemathesis.pytest.from_fixture("api_schema")


@schema.parametrize()
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_no_server_errors_and_responses_match_the_schema(case: Any, cookie: str) -> None:
    response = case.call(headers={"Cookie": cookie})
    case.validate_response(
        response,
        checks=[
            schemathesis.checks.not_a_server_error,
            response_schema_conformance,
        ],
    )
