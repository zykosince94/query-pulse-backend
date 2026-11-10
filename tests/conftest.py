from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from query_pulse.accounts.bootstrap import bootstrap_users
from query_pulse.config import Settings
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.main import create_app

ROOT = Path(__file__).resolve().parent.parent
ADMIN_PASSWORD = "admin-password-for-tests"
DEMO_PASSWORD = "demo-password-for-tests"


def make_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": database_url,
        "session_secret": SecretStr("s" * 48),
        "fernet_key": SecretStr(Fernet.generate_key().decode()),
        "admin_username": "admin",
        "admin_password": SecretStr(ADMIN_PASSWORD),
        "demo_enabled": True,
        "demo_username": "demo",
        "demo_password": SecretStr(DEMO_PASSWORD),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # _env_file=None ignores a developer's .env


def migrate(database_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def run_bootstrap(settings: Settings) -> dict[str, str]:
    async def _run() -> dict[str, str]:
        engine = create_engine(settings.database_url)
        try:
            async with create_sessionmaker(engine)() as session:
                return await bootstrap_users(session, settings)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    migrate(url)
    return url


@pytest.fixture
def settings(database_url: str) -> Settings:
    settings = make_settings(database_url)
    run_bootstrap(settings)
    return settings


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def login(client: TestClient) -> Callable[[str], TestClient]:
    passwords = {"admin": ADMIN_PASSWORD, "demo": DEMO_PASSWORD}

    def _login(username: str) -> TestClient:
        response = client.post(
            "/api/auth/login", json={"username": username, "password": passwords[username]}
        )
        assert response.status_code == 200, response.text
        return client

    return _login
