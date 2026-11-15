from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal, Protocol

TlsMode = Literal["disable", "require", "verify-full"]


class ErrorCode(enum.StrEnum):
    UNREACHABLE = "unreachable"  # nothing listening, DNS failure, connection refused
    TIMEOUT = "timeout"  # no answer within the connect timeout
    AUTH_FAILED = "auth_failed"  # wrong username or password
    TLS = "tls"  # TLS required/refused, certificate problems
    DATABASE_NOT_FOUND = "database_not_found"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNKNOWN = "unknown"


class AdapterError(Exception):
    """A connection or check failure with a code the UI can act on.

    The message is written for the person fixing it. It must never contain the password.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, repr=False)
class ConnectionParams:
    host: str
    port: int
    database: str
    username: str
    password: str
    tls_mode: TlsMode
    connect_timeout: float

    def __repr__(self) -> str:  # never show the password, even in tracebacks
        return (
            f"ConnectionParams(host={self.host!r}, port={self.port}, database={self.database!r}, "
            f"username={self.username!r}, password='***', tls_mode={self.tls_mode!r})"
        )


@dataclass(frozen=True)
class Capability:
    """One feature on the checklist: does it work here, and if not, how to fix it."""

    feature: str
    label: str
    available: bool
    reason: str | None = None  # why it's unavailable (None when available)
    fix: str | None = None  # the exact grant/setting/command that enables it


@dataclass(frozen=True)
class ProbeResult:
    server_version: str
    latency_ms: float
    capabilities: list[Capability] = field(default_factory=list)


class Adapter(Protocol):
    engine: str

    async def probe(self, params: ConnectionParams) -> ProbeResult:
        """Connect, read the version and check capabilities, all read-only.

        Raises AdapterError when the connection itself fails. Missing capabilities are not
        errors: they're reported in the result with their fix.
        """
        ...
