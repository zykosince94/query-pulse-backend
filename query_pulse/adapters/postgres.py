"""PostgreSQL adapter (psycopg 3, async).

Monitoring connections are read-only by construction: the session sets
``default_transaction_read_only``, a short ``statement_timeout`` and an ``application_name`` the
collector will later use to exclude its own statements.
"""

from __future__ import annotations

import asyncio
import time

import psycopg
from psycopg import sql

from query_pulse.adapters.base import (
    AdapterError,
    Capability,
    ConnectionParams,
    ErrorCode,
    ProbeResult,
)

APPLICATION_NAME = "query-pulse"
MIN_SUPPORTED_VERSION = 160000  # PostgreSQL 16: needed for EXPLAIN (GENERIC_PLAN)

_SSLMODE = {"disable": "disable", "require": "require", "verify-full": "verify-full"}


def _classify(exc: Exception, params: ConnectionParams) -> AdapterError:
    """Map a driver error to a code and a message for the person fixing it.

    Specific causes are checked before generic ones: when a hostname resolves to several
    addresses, psycopg reports every attempt, so a real "password authentication failed" can
    sit next to a "timeout expired" from an address that didn't answer (e.g. IPv6 ::1).
    """
    text = str(exc).lower()
    where = f"{params.host}:{params.port}"
    if "password authentication failed" in text or "authentication failed" in text:
        return AdapterError(
            ErrorCode.AUTH_FAILED,
            f"PostgreSQL rejected the username or password for user '{params.username}'.",
        )
    if "does not exist" in text and "database" in text:
        return AdapterError(
            ErrorCode.DATABASE_NOT_FOUND,
            f"Database '{params.database}' does not exist on {where}.",
        )
    if "ssl" in text or "certificate" in text:
        return AdapterError(
            ErrorCode.TLS,
            f"TLS negotiation with {where} failed ({_first_line(exc)}). Check the TLS mode.",
        )
    if "permission denied" in text or "not permitted to log in" in text or "pg_hba" in text:
        return AdapterError(
            ErrorCode.PERMISSION_DENIED,
            f"PostgreSQL refused the connection for user '{params.username}': {_first_line(exc)}",
        )
    if "failed to resolve host" in text or "could not translate host name" in text:
        return AdapterError(
            ErrorCode.UNREACHABLE,
            f"Host '{params.host}' can't be found (DNS lookup failed). Check the hostname.",
        )
    if isinstance(exc, TimeoutError) or "timeout expired" in text:
        return AdapterError(
            ErrorCode.TIMEOUT,
            f"No answer from {where} within {params.connect_timeout:g}s. Check the host, port "
            "and any firewall between Query Pulse and the database.",
        )
    if (
        "connection refused" in text
        or "name or service not known" in text
        or "no such host" in text
        or "nodename nor servname" in text
        or "could not connect" in text
        or "connection failed" in text
    ):
        return AdapterError(
            ErrorCode.UNREACHABLE,
            f"Can't reach PostgreSQL at {where}. Check the host and port, and that the "
            "database is running.",
        )
    return AdapterError(ErrorCode.UNKNOWN, f"Connection failed: {_first_line(exc)}")


def _first_line(exc: Exception) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


class PostgresAdapter:
    engine = "postgres"

    async def probe(self, params: ConnectionParams) -> ProbeResult:
        started = time.perf_counter()
        try:
            # Belt and braces: psycopg's connect_timeout plus an overall asyncio timeout, so a
            # host that accepts TCP but never speaks can't hang the request.
            async with asyncio.timeout(params.connect_timeout + 1):
                conn = await psycopg.AsyncConnection.connect(
                    host=params.host,
                    port=params.port,
                    dbname=params.database,
                    user=params.username,
                    password=params.password or None,
                    sslmode=_SSLMODE[params.tls_mode],
                    connect_timeout=max(1, round(params.connect_timeout)),
                    application_name=APPLICATION_NAME,
                    options="-c default_transaction_read_only=on -c statement_timeout=5000",
                    autocommit=True,
                )
        except (psycopg.Error, OSError, TimeoutError) as exc:
            raise _classify(exc, params) from None

        latency_ms = (time.perf_counter() - started) * 1000
        try:
            async with conn:
                version_num, version = await self._version(conn)
                capabilities = await self._capabilities(conn, params, version_num)
        except psycopg.Error as exc:
            raise _classify(exc, params) from None

        if version_num < MIN_SUPPORTED_VERSION:
            capabilities.insert(
                0,
                Capability(
                    feature="supported_version",
                    label="Supported PostgreSQL version",
                    available=False,
                    reason=f"PostgreSQL {version} is older than 16, the oldest version tested.",
                    fix="Upgrade to PostgreSQL 16 or later.",
                ),
            )
        return ProbeResult(server_version=version, latency_ms=latency_ms, capabilities=capabilities)

    @staticmethod
    async def _version(conn: psycopg.AsyncConnection[tuple[object, ...]]) -> tuple[int, str]:
        cur = await conn.execute(
            "SELECT current_setting('server_version_num')::int, current_setting('server_version')"
        )
        row = await cur.fetchone()
        assert row is not None  # noqa: S101 - always one row
        return int(str(row[0])), str(row[1]).split()[0]

    @staticmethod
    async def _scalar_bool(conn: psycopg.AsyncConnection[tuple[object, ...]], query: str) -> bool:
        cur = await conn.execute(query)  # fixed SQL, no input
        row = await cur.fetchone()
        return bool(row and row[0])

    @staticmethod
    async def _preloaded(
        conn: psycopg.AsyncConnection[tuple[object, ...]], extension_created: bool
    ) -> bool | None:
        """Is pg_stat_statements loaded by the server? None when this user can't tell.

        Reading `shared_preload_libraries` needs superuser or pg_read_all_settings, so a
        least-privilege monitoring role usually can't. If the extension exists, querying its
        view answers the question for anyone: it errors with "must be loaded via
        shared_preload_libraries" when the library isn't loaded.
        """
        if extension_created:
            try:
                await conn.execute("SELECT 1 FROM pg_stat_statements LIMIT 1")
                return True
            except psycopg.errors.ObjectNotInPrerequisiteState:
                return False
            except psycopg.errors.InsufficientPrivilege:
                pass  # can't read the view; fall through to the setting
        try:
            cur = await conn.execute("SELECT current_setting('shared_preload_libraries')")
            row = await cur.fetchone()
        except psycopg.errors.InsufficientPrivilege:
            return None
        libraries = str(row[0] if row else "").replace(" ", "").split(",")
        return "pg_stat_statements" in libraries

    @staticmethod
    def _statements_capability(
        preloaded: bool | None, extension_created: bool, database: str
    ) -> Capability:
        feature, label = "pg_stat_statements", "Statement statistics (pg_stat_statements)"
        preload_fix = (
            "Add pg_stat_statements to shared_preload_libraries in postgresql.conf "
            "(shared_preload_libraries = 'pg_stat_statements') and restart PostgreSQL."
        )
        create_fix = (
            f"As a superuser, connected to '{database}': CREATE EXTENSION pg_stat_statements;"
        )
        if extension_created and preloaded is not False:
            return Capability(feature, label, True)
        if preloaded is False:
            return Capability(
                feature,
                label,
                False,
                reason="pg_stat_statements is not loaded by the server.",
                fix=preload_fix if extension_created else f"{preload_fix} Then: {create_fix}",
            )
        if preloaded is True:
            return Capability(
                feature,
                label,
                False,
                reason=f"The pg_stat_statements extension isn't created in database '{database}'.",
                fix=create_fix,
            )
        return Capability(
            feature,
            label,
            False,
            reason=(
                f"The pg_stat_statements extension isn't created in database '{database}', and "
                "this user can't see whether the server loads it."
            ),
            fix=f"Make sure the server loads it: {preload_fix} Then: {create_fix}",
        )

    async def _capabilities(
        self,
        conn: psycopg.AsyncConnection[tuple[object, ...]],
        params: ConnectionParams,
        version_num: int,
    ) -> list[Capability]:
        # Each check is its own autocommit statement, so one permission error can't abort the
        # others. Every check must work for the least-privileged user we'd ever see.
        extension_created = await self._scalar_bool(
            conn, "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements')"
        )
        read_all_stats = await self._scalar_bool(
            conn, "SELECT pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')"
        )
        preloaded = await self._preloaded(conn, extension_created)

        user = sql.Identifier(params.username).as_string(conn)
        statements = self._statements_capability(preloaded, extension_created, params.database)

        all_users = Capability(
            feature="read_all_stats",
            label="Statistics for all users' queries",
            available=read_all_stats,
            reason=None
            if read_all_stats
            else (
                "This user can only see its own queries' text and statistics; other users' "
                "queries show as <insufficient privilege>."
            ),
            fix=None if read_all_stats else f"GRANT pg_monitor TO {user};",
        )

        plans_ok = version_num >= MIN_SUPPORTED_VERSION
        plans = Capability(
            feature="generic_plans",
            label="Query plans (EXPLAIN GENERIC_PLAN)",
            available=plans_ok,
            reason=None if plans_ok else "Generic plans need PostgreSQL 16 or later.",
            fix=None if plans_ok else "Upgrade to PostgreSQL 16 or later.",
        )
        return [statements, all_users, plans]
