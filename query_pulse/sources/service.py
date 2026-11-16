"""Data-source logic between the API and the adapters.

This is the only place where a stored credential is decrypted, and only to hand it to an
adapter. Nothing here logs a password; log lines carry the source name and the error code.
"""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.adapters import AdapterError, ConnectionParams, ErrorCode, ProbeResult, adapter_for
from query_pulse.collector.models import CollectorState
from query_pulse.collector.warnings import eviction_warning
from query_pulse.crypto import CredentialCipher
from query_pulse.db import utcnow
from query_pulse.sources.models import DataSource, SourceCapability, SourceStatus
from query_pulse.sources.schemas import (
    CapabilityOut,
    ConnectionErrorOut,
    ConnectionFields,
    ConnectionTestResult,
    SourceDetail,
    SourceFields,
    SourceOut,
)

logger = logging.getLogger(__name__)


def connection_fields(source: DataSource) -> ConnectionFields:
    """A saved source's connection target, for re-using its stored password."""
    return ConnectionFields(
        engine=source.engine,
        host=source.host,
        port=source.port,
        database=source.database,
        username=source.username,
        tls_mode=source.tls_mode,  # constrained on the way in
    )


def params_for(fields: ConnectionFields, password: str, timeout: float) -> ConnectionParams:
    return ConnectionParams(
        host=fields.host,
        port=fields.port,
        database=fields.database,
        username=fields.username,
        password=password,
        tls_mode=fields.tls_mode,
        connect_timeout=timeout,
    )


# Where a stored password is allowed to go. Changing any of these and reusing the saved
# password would let someone send it to a server they control, so a new target needs the
# password typed again. (Database and TLS mode can change: same server, same credentials.)
TARGET_FIELDS = ("engine", "host", "port", "username")
# Everything a connection test depends on. If none of these change (and no new password is
# sent), an update only touches name/description/enabled and needs no connection test, so a
# source whose database is down can still be renamed or disabled.
CONNECTION_FIELDS = (*TARGET_FIELDS, "database", "tls_mode")

RETARGET_MESSAGE = (
    "Enter the password again: the engine, host, port or username changed, and a saved "
    "password is only ever sent to the server it was saved for."
)


# What the collector's baseline snapshot belongs to. If any of these change, the stored
# snapshot describes a different database (or different visibility of it), and comparing
# against it would count every statement's lifetime totals as one interval.
BASELINE_FIELDS = (*TARGET_FIELDS, "database")


def baseline_invalidated(source: DataSource, payload: SourceFields) -> bool:
    """Whether an update makes the collector's stored baseline meaningless.

    Re-enabling counts too: the activity of the whole disabled period would otherwise land in
    a single interval.
    """
    moved = any(getattr(source, f) != getattr(payload, f) for f in BASELINE_FIELDS)
    return moved or (not source.enabled and payload.enabled)


async def reset_baseline(session: AsyncSession, source_id: int) -> None:
    """Forget the collector's last snapshot; its next cycle takes a fresh baseline."""
    await session.execute(
        update(CollectorState)
        .where(CollectorState.source_id == source_id)
        .values(snapshot=None, snapshot_at=None, stats_reset=None, dealloc=None)
    )


def target_changed(source: DataSource, fields: ConnectionFields) -> bool:
    return any(getattr(source, f) != getattr(fields, f) for f in TARGET_FIELDS)


def connection_changed(source: DataSource, fields: ConnectionFields) -> bool:
    return any(getattr(source, f) != getattr(fields, f) for f in CONNECTION_FIELDS)


def reusable_password(
    source: DataSource, fields: ConnectionFields, cipher: CredentialCipher
) -> str:
    """The stored password, if it may be used to connect with `fields`.

    Raises PasswordRequiredError when the target changed and a password is stored.
    """
    if source.password_encrypted and target_changed(source, fields):
        raise PasswordRequiredError(RETARGET_MESSAGE)
    return stored_password(source, cipher)


class PasswordRequiredError(Exception):
    """The request would reuse a stored password for a different server."""


def stored_password(source: DataSource, cipher: CredentialCipher) -> str:
    if not source.password_encrypted:
        return ""
    try:
        return cipher.decrypt(source.password_encrypted)
    except ValueError as exc:
        raise AdapterError(ErrorCode.UNKNOWN, str(exc)) from None


async def probe(fields: ConnectionFields, password: str, connect_timeout: float) -> ProbeResult:
    """Connect and check capabilities. Raises AdapterError if the connection fails."""
    params = params_for(fields, password, connect_timeout)
    return await adapter_for(fields.engine.value).probe(params)


def status_for(result: ProbeResult) -> SourceStatus:
    return (
        SourceStatus.HEALTHY
        if all(c.available for c in result.capabilities)
        else SourceStatus.DEGRADED
    )


def record_success(source: DataSource, result: ProbeResult) -> None:
    now = utcnow()
    source.server_version = result.server_version
    source.status = status_for(result)
    source.last_checked_at = now
    source.last_error = None
    # Update rows in place, matched by feature. Replacing the whole list would make the ORM
    # INSERT the new rows before DELETEing the old ones, violating UNIQUE(source_id, feature).
    # Features no longer reported are dropped from the list (delete-orphan removes them).
    existing = {row.feature: row for row in source.capabilities}
    rows: list[SourceCapability] = []
    for i, cap in enumerate(result.capabilities):
        row = existing.pop(cap.feature, None) or SourceCapability(feature=cap.feature)
        row.position = i
        row.label = cap.label
        row.available = cap.available
        row.reason = cap.reason
        row.fix = cap.fix
        row.checked_at = now
        rows.append(row)
    source.capabilities = rows
    logger.info(
        "source check ok: source=%s engine=%s version=%s status=%s",
        source.name,
        source.engine.value,
        result.server_version,
        source.status.value,
    )


def record_failure(source: DataSource, error: AdapterError) -> None:
    source.status = SourceStatus.FAILING
    source.last_checked_at = utcnow()
    source.last_error = error.message
    logger.warning(
        "source check failed: source=%s engine=%s code=%s",
        source.name,
        source.engine.value,
        error.code.value,
    )


def test_result(result: ProbeResult) -> ConnectionTestResult:
    return ConnectionTestResult(
        ok=True,
        server_version=result.server_version,
        latency_ms=round(result.latency_ms, 1),
        capabilities=[
            CapabilityOut(
                feature=c.feature, label=c.label, available=c.available, reason=c.reason, fix=c.fix
            )
            for c in result.capabilities
        ],
    )


def test_failure(error: AdapterError) -> ConnectionTestResult:
    return ConnectionTestResult(
        ok=False, error=ConnectionErrorOut(code=error.code, message=error.message)
    )


def to_out(source: DataSource) -> SourceOut:
    return SourceOut(
        id=source.id,
        name=source.name,
        engine=source.engine,
        host=source.host,
        port=source.port,
        database=source.database,
        username=source.username,
        tls_mode=source.tls_mode,  # constrained on the way in
        enabled=source.enabled,
        description=source.description,
        has_password=bool(source.password_encrypted),
        server_version=source.server_version,
        status=source.status,
        last_checked_at=source.last_checked_at,
        last_error=source.last_error,
        created_at=source.created_at,
        updated_at=source.updated_at,
        last_collected_at=state.last_success_at if (state := source.collector_state) else None,
        warnings=[w] if (w := eviction_warning(source.collector_state)) else [],
    )


def to_detail(source: DataSource) -> SourceDetail:
    return SourceDetail(
        **to_out(source).model_dump(),
        capabilities=[
            CapabilityOut(
                feature=c.feature, label=c.label, available=c.available, reason=c.reason, fix=c.fix
            )
            for c in source.capabilities
        ],
    )
