from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_collector_status_endpoint():
    response = client.get("/collector/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["read_only"] is True
    assert payload["source_count"] >= 2


def test_workload_preview_for_postgres():
    response = client.get("/sources/demo-postgres/workload-preview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["engine"] == "postgres"
    assert payload["read_only"] is True
    assert payload["summary"]["total_queries"] >= 1
    assert len(payload["observations"]) >= 1
