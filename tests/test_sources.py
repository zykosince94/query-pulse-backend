"""Phase 1 placeholder source endpoints. Phase 2 replaces these with real adapters."""

from collections.abc import Callable

from fastapi.testclient import TestClient

MARKER = "MARKER-PASSWORD-d7f3a1"

NEW_SOURCE = {
    "id": "local-postgres",
    "name": "Local Postgres",
    "type": "postgres",
    "host": "localhost",
    "port": 5432,
    "database": "shop",
    "username": "monitor",
    "password": MARKER,
}


def test_lists_demo_sources_without_credentials(login: Callable[[str], TestClient]) -> None:
    response = login("demo").get("/api/sources")
    assert response.status_code == 200
    payload = response.json()
    assert {item["type"] for item in payload["items"]} == {"postgres", "mongodb"}
    assert all("password" not in item for item in payload["items"])


def test_created_source_never_echoes_its_password(login: Callable[[str], TestClient]) -> None:
    client = login("admin")
    created = client.post("/api/sources", json=NEW_SOURCE)
    assert created.status_code == 201
    assert created.json()["has_password"] is True

    listed = client.get("/api/sources")
    for response in (created, listed):
        assert MARKER not in response.text


def test_duplicate_source_is_a_conflict(login: Callable[[str], TestClient]) -> None:
    client = login("admin")
    assert client.post("/api/sources", json=NEW_SOURCE).status_code == 201
    assert client.post("/api/sources", json=NEW_SOURCE).status_code == 409


def test_unknown_source_is_404(login: Callable[[str], TestClient]) -> None:
    client = login("admin")
    assert client.get("/api/sources/nope/health").status_code == 404
    assert client.get("/api/sources/nope/workload-preview").status_code == 404


def test_health_is_honest_about_not_checking(login: Callable[[str], TestClient]) -> None:
    payload = login("admin").get("/api/sources/demo-postgres/health").json()
    assert payload["status"] == "unknown"
    assert payload["ready"] is False


def test_workload_preview_is_read_only(login: Callable[[str], TestClient]) -> None:
    payload = login("demo").get("/api/sources/demo-postgres/workload-preview").json()
    assert payload["engine"] == "postgres"
    assert payload["read_only"] is True
    assert payload["summary"]["total_queries"] == len(payload["observations"])


def test_sources_are_isolated_between_apps(login: Callable[[str], TestClient]) -> None:
    # Each app instance gets its own registry, so tests can't leak state into each other.
    assert login("admin").get("/api/sources").json()["count"] == 2
