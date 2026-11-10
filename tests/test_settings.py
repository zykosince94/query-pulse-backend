from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from tests.conftest import make_settings

URL = "sqlite+aiosqlite:///:memory:"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"session_secret": SecretStr("too-short")}, "session_secret"),
        ({"fernet_key": SecretStr("not-a-fernet-key")}, "Fernet"),
        ({"admin_password": SecretStr("short")}, "admin_password"),
        ({"demo_enabled": True, "demo_password": None}, "QP_DEMO_PASSWORD"),
        ({"demo_username": "admin"}, "QP_DEMO_USERNAME"),
        ({"environment": "staging"}, "environment"),
    ],
)
def test_invalid_settings_fail_fast(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(URL, **overrides)


def test_missing_required_secrets_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    from query_pulse.config import Settings

    for name in ("QP_SESSION_SECRET", "QP_FERNET_KEY", "QP_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert {"session_secret", "fernet_key", "admin_password"} <= missing


def test_secrets_are_not_shown_in_repr() -> None:
    settings = make_settings(URL)
    assert "admin-password-for-tests" not in repr(settings)
    assert settings.session_secret.get_secret_value() not in repr(settings)
