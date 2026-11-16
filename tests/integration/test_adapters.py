"""Adapters against real servers: the permission matrix (QP-AC-06) and connection failures
within the timeout (QP-AC-07)."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import pytest
from pymongo import MongoClient

from query_pulse.adapters import AdapterError, ConnectionParams, ErrorCode, ProbeResult, adapter_for
from tests.integration.conftest import MongoServer, Server

TIMEOUT = 3.0


def major(version: str) -> int:
    return int(version.split(".")[0])


def probe(engine: str, server: Server, database: str, user: str, password: str) -> ProbeResult:
    params = ConnectionParams(
        host=server.host,
        port=server.port,
        database=database,
        username=user,
        password=password,
        tls_mode="disable",
        connect_timeout=TIMEOUT,
    )
    return asyncio.run(adapter_for(engine).probe(params))


def capability(result: ProbeResult, feature: str) -> tuple[bool, str | None]:
    found = next(c for c in result.capabilities if c.feature == feature)
    return found.available, found.fix


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- PostgreSQL permission matrix -----------------------------------------------------------


def test_pg_full_rights_everything_available(pg_preloaded: Server) -> None:
    result = probe("postgres", pg_preloaded, "full_db", "monitor", "monitor-pass")
    assert major(result.server_version) >= 16  # the version matrix also runs the latest
    assert all(c.available for c in result.capabilities), result.capabilities


def test_pg_extension_not_loaded(pg_plain: Server) -> None:
    result = probe("postgres", pg_plain, "test", "monitor", "monitor-pass")
    available, fix = capability(result, "pg_stat_statements")
    assert available is False
    assert fix is not None and "shared_preload_libraries" in fix


def test_pg_extension_not_created(pg_preloaded: Server) -> None:
    result = probe("postgres", pg_preloaded, "noext", "monitor", "monitor-pass")
    available, fix = capability(result, "pg_stat_statements")
    assert available is False
    assert fix is not None and "CREATE EXTENSION pg_stat_statements" in fix and "noext" in fix


def test_pg_missing_monitoring_role(pg_preloaded: Server) -> None:
    result = probe("postgres", pg_preloaded, "full_db", "plain", "plain-pass")
    assert capability(result, "pg_stat_statements")[0] is True
    available, fix = capability(result, "read_all_stats")
    assert available is False
    assert fix == 'GRANT pg_monitor TO "plain";'


def test_pg_session_is_read_only(pg_preloaded: Server) -> None:
    """The adapter's sessions default to read-only, so nothing it runs can write."""
    import psycopg

    from query_pulse.adapters.postgres import APPLICATION_NAME

    async def attempt_write() -> str:
        conn = await psycopg.AsyncConnection.connect(
            host=pg_preloaded.host,
            port=pg_preloaded.port,
            dbname="full_db",
            user="monitor",
            password="monitor-pass",
            options="-c default_transaction_read_only=on",
            application_name=APPLICATION_NAME,
            autocommit=True,
        )
        async with conn:
            try:
                await conn.execute("CREATE TABLE should_fail (id int)")
            except psycopg.Error as exc:
                return type(exc).__name__
        return "wrote"

    assert asyncio.run(attempt_write()) == "ReadOnlySqlTransaction"


# --- PostgreSQL connection failures (QP-AC-07) ----------------------------------------------


def expect_error(
    engine: str, server: Server, database: str, user: str, password: str
) -> tuple[ErrorCode, float]:
    started = time.perf_counter()
    with pytest.raises(AdapterError) as excinfo:
        probe(engine, server, database, user, password)
    elapsed = time.perf_counter() - started
    if password:
        assert password not in excinfo.value.message
    return excinfo.value.code, elapsed


def test_pg_wrong_password(pg_preloaded: Server) -> None:
    code, elapsed = expect_error("postgres", pg_preloaded, "full_db", "monitor", "WRONG-pass")
    assert code is ErrorCode.AUTH_FAILED
    assert elapsed < TIMEOUT


def test_pg_unknown_database(pg_preloaded: Server) -> None:
    code, _ = expect_error("postgres", pg_preloaded, "nope", "monitor", "monitor-pass")
    assert code is ErrorCode.DATABASE_NOT_FOUND


def test_pg_closed_port_fails_within_the_timeout() -> None:
    code, elapsed = expect_error(
        "postgres", Server("127.0.0.1", free_port()), "x", "u", "unused-pw-123"
    )
    assert code in {ErrorCode.UNREACHABLE, ErrorCode.TIMEOUT}
    assert elapsed < TIMEOUT + 2


def test_pg_unresolvable_host() -> None:
    code, _ = expect_error(
        "postgres", Server("no-such-host.invalid", 5432), "x", "u", "unused-pw-123"
    )
    assert code is ErrorCode.UNREACHABLE


# --- MongoDB permission matrix --------------------------------------------------------------


def test_mongo_profiler_off_shows_the_fix(mongo: MongoServer) -> None:
    result = probe("mongodb", mongo, "shop", "monitor", "monitor-pass")
    assert major(result.server_version) >= 7
    assert capability(result, "server_status")[0] is True
    assert capability(result, "explain")[0] is True
    available, fix = capability(result, "profiler")
    assert available is False
    assert fix is not None and "setProfilingLevel(1" in fix


def test_mongo_profiler_on_is_detected(mongo: MongoServer) -> None:
    # Enable profiling on its own database (as root), so other tests still see it off on `shop`.
    client: MongoClient[dict[str, Any]] = MongoClient(mongo.root_url)
    with client:
        client.inventory.command("profile", 1, slowms=100)
    result = probe("mongodb", mongo, "inventory", mongo.root_user, mongo.root_password)
    assert capability(result, "profiler")[0] is True


def test_mongo_missing_cluster_monitor(mongo: MongoServer) -> None:
    result = probe("mongodb", mongo, "shop", "reader", "reader-pass")
    available, fix = capability(result, "server_status")
    assert available is False
    assert fix is not None and "clusterMonitor" in fix and '"reader"' in fix


def test_mongo_wrong_password(mongo: MongoServer) -> None:
    code, elapsed = expect_error("mongodb", mongo, "shop", "monitor", "WRONG-pass")
    assert code is ErrorCode.AUTH_FAILED
    assert elapsed < TIMEOUT


def test_mongo_closed_port_fails_within_the_timeout() -> None:
    code, elapsed = expect_error("mongodb", Server("127.0.0.1", free_port()), "shop", "", "")
    assert code in {ErrorCode.UNREACHABLE, ErrorCode.TIMEOUT}
    assert elapsed < TIMEOUT + 6


# --- TLS (review fix 2) ----------------------------------------------------------------------


def probe_tls(server: Server, tls_mode: str) -> ProbeResult:
    params = ConnectionParams(
        host=server.host,
        port=server.port,
        database="test",
        username="monitor",
        password="monitor-pass",
        tls_mode=tls_mode,  # type: ignore[arg-type]
        connect_timeout=TIMEOUT,
    )
    return asyncio.run(adapter_for("postgres").probe(params))


def test_pg_tls_require_encrypts(pg_tls: Server) -> None:
    assert major(probe_tls(pg_tls, "require").server_version) >= 16


def test_pg_tls_verify_full_rejects_an_untrusted_certificate(pg_tls: Server) -> None:
    """Verification really runs against the system CAs (not a missing root.crt file)."""
    with pytest.raises(AdapterError) as excinfo:
        probe_tls(pg_tls, "verify-full")
    assert excinfo.value.code is ErrorCode.TLS
    assert "root.crt" not in excinfo.value.message
    assert "certificate" in excinfo.value.message.lower()


def test_pg_tls_required_by_client_but_server_has_none(pg_plain: Server) -> None:
    with pytest.raises(AdapterError) as excinfo:
        probe_tls(pg_plain, "require")
    assert excinfo.value.code is ErrorCode.TLS
