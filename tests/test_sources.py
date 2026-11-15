"""Data-source API rules, with a fake adapter (no databases needed).

Real-database behaviour (permissions, timeouts) lives in tests/integration/.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from query_pulse.adapters import AdapterError, Capability, ConnectionParams, ErrorCode, ProbeResult
from query_pulse.sources import service

MARKER = "MARKER-PASSWORD-7f3c9e"


@dataclass
class FakeAdapter:
    """Behaves according to the host name, and records what it was asked to connect with."""

    calls: list[ConnectionParams] = field(default_factory=list)

    async def probe(self, params: ConnectionParams) -> ProbeResult:
        self.calls.append(params)
        if params.host == "badpass.example":
            raise AdapterError(
                ErrorCode.AUTH_FAILED, "PostgreSQL rejected the username or password."
            )
        if params.host == "down.example":
            raise AdapterError(ErrorCode.TIMEOUT, "No answer from down.example:5432 within 5s.")
        degraded = params.host == "degraded.example"
        return ProbeResult(
            server_version="16.4",
            latency_ms=3.2,
            capabilities=[
                Capability("pg_stat_statements", "Statement statistics", True),
                Capability(
                    "read_all_stats",
                    "Statistics for all users' queries",
                    not degraded,
                    reason="Own queries only." if degraded else None,
                    fix="GRANT pg_monitor TO monitor;" if degraded else None,
                ),
            ],
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAdapter]:
    adapter = FakeAdapter()
    monkeypatch.setattr(service, "adapter_for", lambda engine: adapter)
    yield adapter


@pytest.fixture
def admin(login: Callable[[str], TestClient], fake: FakeAdapter) -> TestClient:
    return login("admin")


def source(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Orders DB",
        "engine": "postgres",
        "host": "ok.example",
        "port": 5432,
        "database": "shop",
        "username": "monitor",
        "password": "s3cret",
        "tls_mode": "require",
    }
    body.update(overrides)
    return body


def create(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/api/sources", json=source(**overrides))
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


# --- create (QP-AC-05 / QP-AC-07 shape) -------------------------------------------------------


def test_starts_with_no_sources(admin: TestClient) -> None:
    assert admin.get("/api/sources").json() == []


def test_create_tests_the_connection_then_saves(admin: TestClient, fake: FakeAdapter) -> None:
    created = create(admin)
    assert created["status"] == "healthy"
    assert created["server_version"] == "16.4"
    assert created["has_password"] is True
    assert "password" not in created
    assert [c["feature"] for c in created["capabilities"]] == [
        "pg_stat_statements",
        "read_all_stats",
    ]
    assert fake.calls[-1].password == "s3cret"
    assert fake.calls[-1].tls_mode == "require"
    assert [s["name"] for s in admin.get("/api/sources").json()] == ["Orders DB"]


def test_missing_capability_marks_the_source_degraded(admin: TestClient) -> None:
    created = create(admin, host="degraded.example")
    assert created["status"] == "degraded"
    missing = [c for c in created["capabilities"] if not c["available"]]
    assert missing == [
        {
            "feature": "read_all_stats",
            "label": "Statistics for all users' queries",
            "available": False,
            "reason": "Own queries only.",
            "fix": "GRANT pg_monitor TO monitor;",
        }
    ]


@pytest.mark.parametrize("host", ["badpass.example", "down.example"])
def test_failed_connection_saves_nothing(admin: TestClient, host: str) -> None:
    response = admin.post("/api/sources", json=source(host=host))
    assert response.status_code == 400
    assert response.json()["detail"]
    assert admin.get("/api/sources").json() == []


def test_names_are_unique_ignoring_case(admin: TestClient, fake: FakeAdapter) -> None:
    create(admin)
    calls_before = len(fake.calls)
    response = admin.post("/api/sources", json=source(name="orders db"))
    assert response.status_code == 409
    assert len(fake.calls) == calls_before, "no connection attempt for a duplicate name"


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"username": ""}, "username"),  # PostgreSQL needs a username
        ({"host": "postgres://ok.example"}, "host"),
        ({"host": "ok example"}, "host"),
        ({"port": 70000}, "port"),
        ({"name": " a "}, "name"),
        ({"tls_mode": "maybe"}, "tls_mode"),
        ({"engine": "oracle"}, "engine"),
    ],
)
def test_invalid_input_is_rejected(
    admin: TestClient, overrides: dict[str, Any], field_name: str
) -> None:
    response = admin.post("/api/sources", json=source(**overrides))
    assert response.status_code == 422
    assert field_name in response.text


def test_mongodb_without_auth_needs_no_username(admin: TestClient) -> None:
    created = create(admin, engine="mongodb", port=27017, username="", password="")
    assert created["has_password"] is False
    assert created["username"] == ""


def test_password_is_encrypted_at_rest(admin: TestClient, database_url: str) -> None:
    create(admin, password=MARKER)
    engine = create_engine(database_url.replace("+aiosqlite", ""))
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT password_encrypted FROM data_sources")).scalar_one()
    engine.dispose()
    assert MARKER not in stored
    assert stored.startswith("gAAAA")  # a Fernet token


# --- read --------------------------------------------------------------------------------------


def test_detail_includes_the_checklist(admin: TestClient) -> None:
    created = create(admin)
    detail = admin.get(f"/api/sources/{created['id']}").json()
    assert detail["capabilities"] == created["capabilities"]


def test_unknown_source_is_404(admin: TestClient) -> None:
    assert admin.get("/api/sources/999").status_code == 404
    assert admin.post("/api/sources/999/check").status_code == 404
    assert admin.delete("/api/sources/999").status_code == 404


def test_demo_user_can_read_sources(login: Callable[[str], TestClient], fake: FakeAdapter) -> None:
    client = login("admin")
    created = create(client)
    client.post("/api/auth/logout")
    demo = login("demo")
    assert demo.get("/api/sources").status_code == 200
    assert demo.get(f"/api/sources/{created['id']}").status_code == 200


# --- update ------------------------------------------------------------------------------------


def update_body(**overrides: Any) -> dict[str, Any]:
    body = source(**overrides)
    body.pop("password", None)
    if "password" in overrides:
        body["password"] = overrides["password"]
    return body


def test_update_keeps_the_stored_password_when_omitted(
    admin: TestClient, fake: FakeAdapter
) -> None:
    created = create(admin)
    response = admin.put(f"/api/sources/{created['id']}", json=update_body(database="sales"))
    assert response.status_code == 200
    assert response.json()["database"] == "sales"
    assert response.json()["has_password"] is True
    assert fake.calls[-1].password == "s3cret"


def test_update_can_change_or_remove_the_password(admin: TestClient, fake: FakeAdapter) -> None:
    created = create(admin)
    url = f"/api/sources/{created['id']}"
    admin.put(url, json=update_body(password="n3w"))
    assert fake.calls[-1].password == "n3w"
    removed = admin.put(url, json=update_body(password="")).json()
    assert removed["has_password"] is False


def test_failed_update_changes_nothing(admin: TestClient) -> None:
    created = create(admin)
    url = f"/api/sources/{created['id']}"
    response = admin.put(url, json=update_body(host="badpass.example"))
    assert response.status_code == 400
    assert admin.get(url).json()["host"] == "ok.example"


def test_update_cannot_take_another_sources_name(admin: TestClient) -> None:
    create(admin, name="First")
    second = create(admin, name="Second")
    response = admin.put(f"/api/sources/{second['id']}", json=update_body(name="FIRST"))
    assert response.status_code == 409


# --- test connection ---------------------------------------------------------------------------


def test_connection_test_reports_success_without_saving(admin: TestClient) -> None:
    body = source()
    body.pop("name")
    result = admin.post("/api/sources/test", json=body).json()
    assert result["ok"] is True
    assert result["server_version"] == "16.4"
    assert len(result["capabilities"]) == 2
    assert admin.get("/api/sources").json() == []


def test_connection_test_failure_is_a_result_not_an_error(admin: TestClient) -> None:
    body = source(host="badpass.example")
    body.pop("name")
    response = admin.post("/api/sources/test", json=body)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"]["code"] == "auth_failed"


def test_connection_test_can_use_a_stored_password(admin: TestClient, fake: FakeAdapter) -> None:
    created = create(admin)
    body = source(source_id=created["id"])
    body.pop("name")
    body.pop("password")
    assert admin.post("/api/sources/test", json=body).json()["ok"] is True
    assert fake.calls[-1].password == "s3cret"


# --- re-check and delete -----------------------------------------------------------------------


def test_recheck_records_failure_then_recovery(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = create(admin)
    url = f"/api/sources/{created['id']}/check"

    async def failing(params: ConnectionParams) -> ProbeResult:
        raise AdapterError(ErrorCode.UNREACHABLE, "Can't reach PostgreSQL at ok.example:5432.")

    broken = FakeAdapter()
    monkeypatch.setattr(broken, "probe", failing)
    monkeypatch.setattr(service, "adapter_for", lambda engine: broken)
    failed = admin.post(url).json()
    assert failed["status"] == "failing"
    assert failed["last_error"] == "Can't reach PostgreSQL at ok.example:5432."
    assert failed["capabilities"] == created["capabilities"], "last known checklist is kept"

    monkeypatch.setattr(service, "adapter_for", lambda engine: FakeAdapter())
    recovered = admin.post(url).json()
    assert recovered["status"] == "healthy"
    assert recovered["last_error"] is None


def test_delete_removes_the_source(admin: TestClient) -> None:
    created = create(admin)
    assert admin.delete(f"/api/sources/{created['id']}").status_code == 204
    assert admin.get(f"/api/sources/{created['id']}").status_code == 404
    assert admin.get("/api/sources").json() == []


def test_collector_status_counts_sources(admin: TestClient) -> None:
    create(admin)
    status = admin.get("/api/collector/status").json()
    assert status == {
        "status": "not_running",
        "source_count": 1,
        "message": "The collector process arrives in Phase 3.",
    }


# --- QP-AC-08: a marker password never appears in a response body or a log line -----------------


def test_marker_password_never_leaks(admin: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    responses = []

    created = admin.post("/api/sources", json=source(password=MARKER))
    responses.append(created)
    source_id = created.json()["id"]
    responses.append(admin.get("/api/sources"))
    responses.append(admin.get(f"/api/sources/{source_id}"))
    test_body = source(password=MARKER)
    test_body.pop("name")
    responses.append(admin.post("/api/sources/test", json=test_body))
    test_body["host"] = "badpass.example"
    responses.append(admin.post("/api/sources/test", json=test_body))
    responses.append(
        admin.post("/api/sources", json=source(name="Other", host="down.example", password=MARKER))
    )
    responses.append(admin.put(f"/api/sources/{source_id}", json=update_body(password=MARKER)))
    responses.append(
        admin.put(
            f"/api/sources/{source_id}", json=update_body(host="badpass.example", password=MARKER)
        )
    )
    responses.append(admin.post(f"/api/sources/{source_id}/check"))
    # Validation errors echo input back; the password must not be among it.
    responses.append(admin.post("/api/sources", json=source(port=0, password=MARKER)))

    for response in responses:
        assert MARKER not in response.text, f"{response.request.method} {response.request.url}"
    for record in caplog.records:
        assert MARKER not in record.getMessage()
        assert MARKER not in str(record.args)
    assert any("source check" in r.getMessage() for r in caplog.records), "checks are logged"
