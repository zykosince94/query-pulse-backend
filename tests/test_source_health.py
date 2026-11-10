from app import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_sources_endpoint_lists_demo_sources():
    response = client.get("/sources")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] >= 2
    assert any(item["type"] == "postgres" for item in payload["items"])
    assert any(item["type"] == "mongodb" for item in payload["items"])


def test_test_source_connection_accepts_local_postgres_credentials():
    response = client.post(
        "/sources/test",
        json={
            "name": "Local Postgres",
            "type": "postgres",
            "host": "localhost",
            "port": 5432,
            "database": "querypulse",
            "username": "postgres",
            "password": "postgres",
            "ssl": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["ready"] is True


def test_source_health_route_reports_ready_state():
    response = client.get("/sources/demo-postgres/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["ready"] is True
    assert payload["engine"] == "postgres"
