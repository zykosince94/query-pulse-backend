"""Access rules derived from the OpenAPI spec, so new endpoints are covered automatically.

QP-AC-02: protected endpoints return 401 without a session.
QP-AC-03: the demo user gets 403 on every write endpoint.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_settings

# Signing out is public: it must work for the demo user, and with no session it's a no-op.
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _operations() -> list[tuple[str, str]]:
    from query_pulse.main import create_app

    app = create_app(make_settings("sqlite+aiosqlite:///:memory:"))
    spec: dict[str, Any] = app.openapi()
    return sorted(
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method in item
        if method.upper() in WRITE_METHODS | {"GET"}
    )


OPERATIONS = _operations()
PROTECTED = [op for op in OPERATIONS if op not in PUBLIC]
WRITES = [op for op in OPERATIONS if op[0] in WRITE_METHODS and op not in PUBLIC]


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "demo-postgres", path)


def test_spec_has_write_endpoints_to_check() -> None:
    assert WRITES, "expected at least one write endpoint in openapi.json"


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
def test_protected_endpoint_requires_a_session(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, _url(path), json={})
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(("method", "path"), WRITES, ids=[f"{m} {p}" for m, p in WRITES])
def test_demo_user_is_forbidden_from_writes(
    login: Callable[[str], TestClient], method: str, path: str
) -> None:
    client = login("demo")
    response = client.request(method, _url(path), json={})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(("method", "path"), WRITES, ids=[f"{m} {p}" for m, p in WRITES])
def test_admin_is_not_forbidden_from_writes(
    login: Callable[[str], TestClient], method: str, path: str
) -> None:
    client = login("admin")
    response = client.request(method, _url(path), json={})
    assert response.status_code not in (401, 403), response.text
