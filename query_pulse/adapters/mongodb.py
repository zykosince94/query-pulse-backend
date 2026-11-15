"""MongoDB adapter (PyMongo's native async client).

Query Pulse connects directly to the given node (``directConnection``): it monitors one
server and must not follow a replica set to hostnames it can't reach. Nothing here writes;
the profiler level is only *read*, never changed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.errors import (
    ConfigurationError,
    ConnectionFailure,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from query_pulse.adapters.base import (
    AdapterError,
    Capability,
    ConnectionParams,
    ErrorCode,
    ProbeResult,
)

APPLICATION_NAME = "query-pulse"
MIN_SUPPORTED_MAJOR = 7
UNAUTHORIZED = {13, 18}  # Unauthorized, AuthenticationFailed


def _classify(exc: Exception, params: ConnectionParams) -> AdapterError:
    where = f"{params.host}:{params.port}"
    text = str(exc).lower()
    if isinstance(exc, OperationFailure) and exc.code == 18:
        return AdapterError(
            ErrorCode.AUTH_FAILED,
            f"MongoDB rejected the username or password for user '{params.username}'.",
        )
    if isinstance(exc, OperationFailure) and exc.code == 13:
        return AdapterError(
            ErrorCode.PERMISSION_DENIED,
            f"User '{params.username}' isn't allowed to run the checks: {exc.details or exc}",
        )
    if "ssl" in text or "tls" in text or "certificate" in text:
        return AdapterError(
            ErrorCode.TLS, f"TLS negotiation with {where} failed. Check the TLS mode."
        )
    if isinstance(exc, ServerSelectionTimeoutError | TimeoutError):
        refused = "refused" in text or "getaddrinfo" in text or "nodename" in text
        if refused:
            return AdapterError(
                ErrorCode.UNREACHABLE,
                f"Can't reach MongoDB at {where}. Check the host and port, and that the "
                "server is running.",
            )
        return AdapterError(
            ErrorCode.TIMEOUT,
            f"No answer from {where} within {params.connect_timeout:g}s. Check the host, port "
            "and any firewall between Query Pulse and the server.",
        )
    if isinstance(exc, ConnectionFailure):
        return AdapterError(ErrorCode.UNREACHABLE, f"Can't reach MongoDB at {where}.")
    if isinstance(exc, ConfigurationError):
        return AdapterError(ErrorCode.UNKNOWN, f"Invalid connection settings: {exc}")
    return AdapterError(ErrorCode.UNKNOWN, f"Connection failed: {exc}")


class MongoAdapter:
    engine = "mongodb"

    def _client(self, params: ConnectionParams) -> AsyncMongoClient[dict[str, Any]]:
        timeout_ms = int(params.connect_timeout * 1000)
        options: dict[str, Any] = {
            "host": params.host,
            "port": params.port,
            "directConnection": True,
            "appname": APPLICATION_NAME,
            "connectTimeoutMS": timeout_ms,
            "serverSelectionTimeoutMS": timeout_ms,
            "socketTimeoutMS": 5000,
            "tls": params.tls_mode != "disable",
        }
        if params.tls_mode == "require":
            options["tlsAllowInvalidCertificates"] = True  # encrypt, don't verify
        if params.username:
            options["username"] = params.username
            options["password"] = params.password
            options["authSource"] = "admin"
        return AsyncMongoClient(**options)

    async def probe(self, params: ConnectionParams) -> ProbeResult:
        started = time.perf_counter()
        client = self._client(params)
        try:
            async with asyncio.timeout(params.connect_timeout + 5):
                # `ping` forces server selection and authentication.
                await client.admin.command("ping")
                latency_ms = (time.perf_counter() - started) * 1000
                build = await client.admin.command("buildInfo")
                version = str(build.get("version", "unknown"))
                capabilities = await self._capabilities(client, params)
        except (PyMongoError, TimeoutError) as exc:
            raise _classify(exc, params) from None
        finally:
            await client.close()

        major = int(version.split(".")[0]) if version[:1].isdigit() else 0
        if major < MIN_SUPPORTED_MAJOR:
            capabilities.insert(
                0,
                Capability(
                    feature="supported_version",
                    label="Supported MongoDB version",
                    available=False,
                    reason=f"MongoDB {version} is older than 7.0, the oldest version tested.",
                    fix="Upgrade to MongoDB 7.0 or later.",
                ),
            )
        return ProbeResult(server_version=version, latency_ms=latency_ms, capabilities=capabilities)

    async def _capabilities(
        self, client: AsyncMongoClient[dict[str, Any]], params: ConnectionParams
    ) -> list[Capability]:
        user = params.username or "<user>"
        db = client[params.database]
        grant_cluster_monitor = (
            f'db.getSiblingDB("admin").grantRolesToUser("{user}", '
            '[{ role: "clusterMonitor", db: "admin" }])'
        )
        grant_read = (
            f'db.getSiblingDB("admin").grantRolesToUser("{user}", '
            f'[{{ role: "read", db: "{params.database}" }}])'
        )

        # Server metrics: serverStatus needs clusterMonitor.
        try:
            await client.admin.command("serverStatus")
            server_status = Capability("server_status", "Server metrics (serverStatus)", True)
        except OperationFailure as exc:
            if exc.code not in UNAUTHORIZED:
                raise
            server_status = Capability(
                "server_status",
                "Server metrics (serverStatus)",
                False,
                reason="This user can't run serverStatus.",
                fix=grant_cluster_monitor,
            )

        # Query plans: explain at queryPlanner verbosity needs read on the database.
        try:
            await db.list_collection_names()
            explain = Capability("explain", "Query plans (explain)", True)
        except OperationFailure as exc:
            if exc.code not in UNAUTHORIZED:
                raise
            explain = Capability(
                "explain",
                "Query plans (explain)",
                False,
                reason=f"This user can't read database '{params.database}'.",
                fix=grant_read,
            )

        # Profiler: opt-in by the user; Query Pulse never enables it.
        enable_profiler = (
            f'Connected to "{params.database}": db.setProfilingLevel(1, {{ slowms: 100 }}). '
            "Profiling adds some overhead; Query Pulse only reads it and never turns it on."
        )
        try:
            status = await db.command("profile", -1)
            level = int(status.get("was", 0))
            profiler = (
                Capability("profiler", "Slow operations (profiler)", True)
                if level > 0
                else Capability(
                    "profiler",
                    "Slow operations (profiler)",
                    False,
                    reason=f"Profiling is off on database '{params.database}'.",
                    fix=enable_profiler,
                )
            )
        except OperationFailure as exc:
            if exc.code not in UNAUTHORIZED:
                raise
            profiler = Capability(
                "profiler",
                "Slow operations (profiler)",
                False,
                reason="This user can't read the profiling level.",
                fix=f"{grant_read}, and enable profiling: {enable_profiler}",
            )

        return [server_status, explain, profiler]
