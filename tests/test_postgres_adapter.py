"""Unit tests for PostgreSQL adapter settings (no database needed)."""

from typing import Any

import psycopg
import pytest

from query_pulse.adapters import AdapterError, ConnectionParams
from query_pulse.adapters import postgres as pg


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("disable", ("disable", None)),
        ("require", ("require", None)),
        # Review fix 2: verified TLS uses the OS's trusted CAs, not ~/.postgresql/root.crt.
        ("verify-full", ("verify-full", "system")),
    ],
)
def test_tls_options(mode: str, expected: tuple[str, str | None]) -> None:
    assert pg.tls_options(mode) == expected


def test_connect_receives_the_tls_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise OSError("connection refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", fake_connect)
    params = ConnectionParams("db.example", 5432, "shop", "monitor", "pw", "verify-full", 2.0)

    import asyncio

    with pytest.raises(AdapterError):
        asyncio.run(pg.PostgresAdapter().probe(params))
    assert seen["sslmode"] == "verify-full"
    assert seen["sslrootcert"] == "system"
    assert seen["options"] == "-c default_transaction_read_only=on -c statement_timeout=5000"
