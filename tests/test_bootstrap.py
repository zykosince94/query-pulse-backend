from fastapi.testclient import TestClient
from pydantic import SecretStr

from query_pulse.main import create_app
from tests.conftest import ADMIN_PASSWORD, DEMO_PASSWORD, make_settings, run_bootstrap


def test_bootstrap_creates_admin_and_demo(database_url: str) -> None:
    result = run_bootstrap(make_settings(database_url))
    assert result == {"admin": "created", "demo": "created"}


def test_bootstrap_is_idempotent(database_url: str) -> None:
    settings = make_settings(database_url)
    run_bootstrap(settings)
    assert run_bootstrap(settings) == {"admin": "unchanged", "demo": "unchanged"}


def test_bootstrap_updates_a_changed_password(database_url: str) -> None:
    run_bootstrap(make_settings(database_url))
    result = run_bootstrap(
        make_settings(database_url, admin_password=SecretStr("a-new-admin-password"))
    )
    assert result["admin"] == "updated"


def test_bootstrap_removes_demo_user_when_disabled(database_url: str) -> None:
    run_bootstrap(make_settings(database_url))
    result = run_bootstrap(make_settings(database_url, demo_enabled=False, demo_password=None))
    assert result == {"admin": "unchanged", "demo": "removed"}


def test_renaming_the_admin_removes_the_old_account(database_url: str) -> None:
    run_bootstrap(make_settings(database_url))
    renamed = make_settings(database_url, admin_username="root")
    assert run_bootstrap(renamed) == {"root": "created", "admin": "removed", "demo": "unchanged"}

    with TestClient(create_app(renamed)) as client:
        old = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert old.status_code == 401
        new = client.post("/api/auth/login", json={"username": "root", "password": ADMIN_PASSWORD})
        assert new.status_code == 200


def test_renaming_the_demo_user_removes_the_old_account(database_url: str) -> None:
    run_bootstrap(make_settings(database_url))
    renamed = make_settings(database_url, demo_username="guest")
    assert run_bootstrap(renamed) == {"admin": "unchanged", "guest": "created", "demo": "removed"}

    with TestClient(create_app(renamed)) as client:
        old = client.post("/api/auth/login", json={"username": "demo", "password": DEMO_PASSWORD})
        assert old.status_code == 401
        assert client.post("/api/auth/demo").json()["username"] == "guest"


def test_removed_admin_loses_an_existing_session(database_url: str) -> None:
    settings = make_settings(database_url)
    run_bootstrap(settings)
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert client.get("/api/auth/me").status_code == 200

        run_bootstrap(make_settings(database_url, admin_username="root"))
        assert client.get("/api/auth/me").status_code == 401
