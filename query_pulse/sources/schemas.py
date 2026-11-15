"""API shapes for data sources. Credentials go in; they never come out."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from query_pulse.adapters import ErrorCode
from query_pulse.sources.models import SourceEngine, SourceStatus

TlsMode = Literal["disable", "require", "verify-full"]

DEFAULT_PORTS = {SourceEngine.POSTGRES: 5432, SourceEngine.MONGODB: 27017}


class ConnectionFields(BaseModel):
    """Everything needed to reach a database, except the password."""

    engine: SourceEngine
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=120)
    username: str = Field(default="", max_length=120)
    tls_mode: TlsMode = "disable"

    @field_validator("host", "database", "username")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("host")
    @classmethod
    def _plain_host(cls, value: str) -> str:
        if not value or any(c.isspace() for c in value) or "://" in value or "/" in value:
            raise ValueError("Enter a hostname or IP address only (no scheme, path or spaces)")
        return value

    @model_validator(mode="after")
    def _postgres_needs_username(self) -> ConnectionFields:
        if self.engine is SourceEngine.POSTGRES and not self.username:
            raise ValueError("PostgreSQL needs a username")
        return self


class SourceFields(ConnectionFields):
    name: str = Field(min_length=2, max_length=120)
    enabled: bool = True
    description: str = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Name must be at least 2 characters")
        return value


class SourceCreate(SourceFields):
    password: SecretStr = Field(default=SecretStr(""), max_length=1024)


class SourceUpdate(SourceFields):
    # Omit (or send null) to keep the stored password; send "" to remove it.
    password: SecretStr | None = Field(default=None, max_length=1024)


class ConnectionTestRequest(ConnectionFields):
    # When editing an existing source, the form usually doesn't have the password: pass
    # `source_id` and omit `password` to test with the stored one.
    password: SecretStr | None = Field(default=None, max_length=1024)
    source_id: int | None = None


class CapabilityOut(BaseModel):
    feature: str
    label: str
    available: bool
    reason: str | None
    fix: str | None


class ConnectionErrorOut(BaseModel):
    code: ErrorCode
    message: str


class ConnectionTestResult(BaseModel):
    ok: bool
    server_version: str | None = None
    latency_ms: float | None = None
    error: ConnectionErrorOut | None = None
    capabilities: list[CapabilityOut] = []


class SourceOut(BaseModel):
    id: int
    name: str
    engine: SourceEngine
    host: str
    port: int
    database: str
    username: str
    tls_mode: TlsMode
    enabled: bool
    description: str
    has_password: bool
    server_version: str | None
    status: SourceStatus
    last_checked_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class SourceDetail(SourceOut):
    capabilities: list[CapabilityOut]


class CollectorStatus(BaseModel):
    status: Literal["not_running"]
    source_count: int
    message: str
