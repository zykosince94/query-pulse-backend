from pydantic import SecretStr

from tests.conftest import make_settings, run_bootstrap


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
    assert result == {"admin": "unchanged", "demo users": "removed"}
