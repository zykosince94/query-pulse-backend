"""MongoDB adapter (PyMongo's native async client).

Query Pulse connects directly to the given node (``directConnection``): it monitors one
server and must not follow a replica set to hostnames it can't reach. Nothing here writes;
the profiler level is only *read*, never changed.

Collection (Phase 6) reads three things, all read-only:

- serverStatus: opcounters, connections, cache (needs clusterMonitor);
- system.profile: the slow operations the user's profiler recorded (needs read on the
  database), excluding Query Pulse's own operations by their appName;
- explain at queryPlanner verbosity: the plan MongoDB would use, without executing.

Profiler entries carry the real query values. They're returned to the collector, which
keeps only shapes and counters (ADR 0010); nothing here logs them.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
# Server-side limit on each read and explain, like PostgreSQL's 5 s statement_timeout.
MAX_TIME_MS = 5000
_PROFILE_FIELDS = {
    "op": 1,
    "ns": 1,
    "command": 1,
    "millis": 1,
    "nreturned": 1,
    "docsExamined": 1,
    "keysExamined": 1,
    "ts": 1,
}
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


@dataclass(frozen=True)
class ProfileEntry:
    """One profiled operation. `command` holds real values: never log or store it."""

    op: str
    ns: str
    millis: float
    nreturned: int
    docs_examined: int
    keys_examined: int
    ts: datetime
    command: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ProfileReading:
    entries: list[ProfileEntry]
    # True when the capped system.profile had already dropped entries newer than the
    # cursor: some operations were never seen.
    overflowed: bool
    # True when more entries were waiting than one cycle reads; the rest come next cycle.
    truncated: bool


@dataclass(frozen=True)
class ServerStatus:
    uptime_seconds: float
    opcounters: dict[str, int]
    connections_current: int | None
    connections_available: int | None
    cache_bytes: int | None
    cache_max_bytes: int | None
    repl_state: str | None


MongoClient = AsyncMongoClient[dict[str, Any]]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _repl_state(status: dict[str, Any]) -> str | None:
    repl = status.get("repl")
    if not isinstance(repl, dict):
        return None
    if repl.get("isWritablePrimary") or repl.get("ismaster"):
        return "primary"
    if repl.get("secondary"):
        return "secondary"
    return "other"


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

    async def connect(self, params: ConnectionParams) -> MongoClient:
        """A connected client (authenticated, server selected), or AdapterError.

        The caller closes it (`await client.close()`).
        """
        client = self._client(params)
        try:
            async with asyncio.timeout(params.connect_timeout + 5):
                await client.admin.command("ping")
        except (PyMongoError, TimeoutError) as exc:
            await client.close()
            raise _classify(exc, params) from None
        return client

    @staticmethod
    async def server_time(client: MongoClient) -> datetime:
        """The server's clock: profiler timestamps are in server time, not ours."""
        hello = await client.admin.command("hello")
        local = hello.get("localTime")
        return _aware(local) if isinstance(local, datetime) else datetime.now(UTC)

    @staticmethod
    async def server_status(client: MongoClient) -> ServerStatus:
        status = await client.admin.command("serverStatus", maxTimeMS=MAX_TIME_MS)
        counters = status.get("opcounters")
        counters = counters if isinstance(counters, dict) else {}
        connections = status.get("connections")
        connections = connections if isinstance(connections, dict) else {}
        wired = status.get("wiredTiger")
        cache = wired.get("cache") if isinstance(wired, dict) else None
        cache = cache if isinstance(cache, dict) else {}
        uptime_ms = _int(status.get("uptimeMillis"))
        return ServerStatus(
            uptime_seconds=uptime_ms / 1000
            if uptime_ms is not None
            else float(status.get("uptime", 0)),
            opcounters={str(k): n for k, v in counters.items() if (n := _int(v)) is not None},
            connections_current=_int(connections.get("current")),
            connections_available=_int(connections.get("available")),
            cache_bytes=_int(cache.get("bytes currently in the cache")),
            cache_max_bytes=_int(cache.get("maximum bytes configured")),
            repl_state=_repl_state(status),
        )

    @staticmethod
    async def profiling_level(client: MongoClient, database: str) -> int:
        status = await client[database].command("profile", -1)
        return int(status.get("was", 0))

    @staticmethod
    async def read_profile(
        client: MongoClient, database: str, after: datetime, until: datetime, limit: int
    ) -> ProfileReading:
        """Profiled operations with after < ts <= until, oldest first, at most `limit`.

        Query Pulse's own operations (this read, explains, checks) are left out by their
        appName, the way PostgreSQL statements are left out by the /* query-pulse */ marker.
        """
        profile = client[database]["system.profile"]
        # system.profile is capped: when it's full, the oldest entries are overwritten. If
        # even the oldest one left is newer than the cursor, some were lost unread.
        oldest = await profile.find_one({}, {"ts": 1}, sort=[("$natural", 1)])
        overflowed = bool(
            oldest is not None
            and isinstance(oldest.get("ts"), datetime)
            and _aware(oldest["ts"]) > after
        )
        cursor = profile.find(
            {"ts": {"$gt": after, "$lte": until}, "appName": {"$ne": APPLICATION_NAME}},
            _PROFILE_FIELDS,
            sort=[("ts", 1)],
            limit=limit + 1,
            max_time_ms=MAX_TIME_MS,
        )
        raw = await cursor.to_list(length=limit + 1)
        entries = [
            ProfileEntry(
                op=str(doc.get("op", "")),
                ns=str(doc.get("ns", "")),
                millis=float(doc.get("millis") or 0),
                nreturned=_int(doc.get("nreturned")) or 0,
                docs_examined=_int(doc.get("docsExamined")) or 0,
                keys_examined=_int(doc.get("keysExamined")) or 0,
                ts=_aware(doc["ts"]),
                command=doc["command"] if isinstance(doc.get("command"), dict) else {},
            )
            for doc in raw[:limit]
            if isinstance(doc.get("ts"), datetime)
        ]
        return ProfileReading(entries, overflowed=overflowed, truncated=len(raw) > limit)

    @staticmethod
    async def explain(
        client: MongoClient, database: str, command: dict[str, Any]
    ) -> dict[str, Any]:
        """explain at queryPlanner verbosity: the chosen plan, nothing executed."""
        result: dict[str, Any] = await client[database].command(
            {"explain": command, "verbosity": "queryPlanner", "maxTimeMS": MAX_TIME_MS}
        )
        return result

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
