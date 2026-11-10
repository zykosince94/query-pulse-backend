from pathlib import Path

from fastapi.testclient import TestClient

from query_pulse.main import create_app
from tests.conftest import make_settings


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_checks_the_database(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_ready_reports_503_when_database_is_unreachable(tmp_path: Path) -> None:
    missing = (tmp_path / "no-such-dir" / "app.db").as_posix()
    settings = make_settings(f"sqlite+aiosqlite:///{missing}")
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
