from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from query_pulse.accounts.dependencies import SessionDep, Writer, current_user
from query_pulse.adapters import AdapterError
from query_pulse.api_types import Id
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.sources import service
from query_pulse.sources.models import DataSource
from query_pulse.sources.schemas import (
    ConnectionFields,
    ConnectionTestRequest,
    ConnectionTestResult,
    SourceCreate,
    SourceDetail,
    SourceOut,
    SourceUpdate,
)

# Every route here requires a session; write routes additionally depend on `Writer`.
router = APIRouter(prefix="/api", tags=["sources"], dependencies=[Depends(current_user)])

CONNECTION_FAILED = {"description": "The connection test failed; nothing was saved"}


def _cipher(request: Request) -> CredentialCipher:
    cipher: CredentialCipher = request.app.state.cipher
    return cipher


def _timeout(request: Request) -> float:
    settings: Settings = request.app.state.settings
    return settings.source_connect_timeout_seconds


async def _get_or_404(session: SessionDep, source_id: int) -> DataSource:
    source = await session.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Source not found")
    return source


async def _ensure_unique_name(
    session: SessionDep, name: str, exclude_id: int | None = None
) -> None:
    query = select(DataSource.id).where(func.lower(DataSource.name) == name.lower())
    if exclude_id is not None:
        query = query.where(DataSource.id != exclude_id)
    if await session.scalar(query) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"A source named '{name}' exists")


async def _commit_or_409(session: SessionDep, name: str) -> None:
    """Commit, turning a unique-name violation into 409.

    `_ensure_unique_name` gives a friendly early answer, but two requests can pass it at the
    same moment; the case-insensitive unique index is what actually guarantees uniqueness.
    """
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"A source named '{name}' exists"
        ) from None


@router.get("/sources", response_model=list[SourceOut], operation_id="listSources")
async def list_sources(session: SessionDep) -> list[SourceOut]:
    sources = await session.scalars(select(DataSource).order_by(func.lower(DataSource.name)))
    return [service.to_out(s) for s in sources]


@router.get("/sources/{source_id}", response_model=SourceDetail, operation_id="getSource")
async def get_source(source_id: Id, session: SessionDep) -> SourceDetail:
    return service.to_detail(await _get_or_404(session, source_id))


@router.post(
    "/sources/test",
    response_model=ConnectionTestResult,
    operation_id="testSourceConnection",
    responses={400: {"description": "A stored password can't be reused for a different server"}},
)
async def test_connection(
    payload: ConnectionTestRequest, request: Request, session: SessionDep, _: Writer
) -> ConnectionTestResult:
    """Try the connection and report capabilities without saving anything.

    A failed connection is a normal result (`ok: false` with an error code and message),
    not an HTTP error.
    """
    if payload.password is not None:
        password = payload.password.get_secret_value()
    elif payload.source_id is not None:
        existing = await _get_or_404(session, payload.source_id)
        try:
            password = service.reusable_password(existing, payload, _cipher(request))
        except service.PasswordRequiredError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
        except AdapterError as error:
            return service.test_failure(error)
    else:
        password = ""
    try:
        result = await service.probe(payload, password, _timeout(request))
    except AdapterError as error:
        return service.test_failure(error)
    return service.test_result(result)


@router.post(
    "/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=SourceDetail,
    operation_id="createSource",
    responses={400: CONNECTION_FAILED, 409: {"description": "Name already in use"}},
)
async def create_source(
    payload: SourceCreate, request: Request, session: SessionDep, _: Writer
) -> SourceDetail:
    """Save a source, but only after a successful connection test."""
    await _ensure_unique_name(session, payload.name)
    password = payload.password.get_secret_value()
    try:
        result = await service.probe(payload, password, _timeout(request))
    except AdapterError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=error.message) from None

    source = DataSource(
        name=payload.name,
        engine=payload.engine,
        host=payload.host,
        port=payload.port,
        database=payload.database,
        username=payload.username,
        password_encrypted=_cipher(request).encrypt(password) if password else None,
        tls_mode=payload.tls_mode,
        enabled=payload.enabled,
        description=payload.description,
    )
    service.record_success(source, result)
    session.add(source)
    await _commit_or_409(session, payload.name)
    await session.refresh(source)
    return service.to_detail(source)


@router.put(
    "/sources/{source_id}",
    response_model=SourceDetail,
    operation_id="updateSource",
    responses={400: CONNECTION_FAILED, 409: {"description": "Name already in use"}},
)
async def update_source(
    source_id: Id, payload: SourceUpdate, request: Request, session: SessionDep, _: Writer
) -> SourceDetail:
    """Replace a source's settings.

    Changing how to connect (engine, host, port, database, username, TLS, password) requires a
    passing connection test. Changing only the name, description or `enabled` doesn't, so a
    source whose database is down can still be renamed or disabled.
    """
    source = await _get_or_404(session, source_id)
    await _ensure_unique_name(session, payload.name, exclude_id=source.id)
    rebaseline = service.baseline_invalidated(source, payload)

    if payload.password is None and not service.connection_changed(source, payload):
        source.name = payload.name
        source.enabled = payload.enabled
        source.description = payload.description
        if rebaseline:
            await service.reset_baseline(session, source.id)
        await _commit_or_409(session, payload.name)
        await session.refresh(source)
        return service.to_detail(source)

    cipher = _cipher(request)
    try:
        password = (
            payload.password.get_secret_value()
            if payload.password is not None
            else service.reusable_password(source, payload, cipher)
        )
        result = await service.probe(payload, password, _timeout(request))
    except service.PasswordRequiredError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except AdapterError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=error.message) from None

    source.name = payload.name
    source.engine = payload.engine
    source.host = payload.host
    source.port = payload.port
    source.database = payload.database
    source.username = payload.username
    source.tls_mode = payload.tls_mode
    source.enabled = payload.enabled
    source.description = payload.description
    if payload.password is not None:
        source.password_encrypted = cipher.encrypt(password) if password else None
    service.record_success(source, result)
    if rebaseline:
        await service.reset_baseline(session, source.id)
    await _commit_or_409(session, payload.name)
    await session.refresh(source)
    return service.to_detail(source)


@router.post("/sources/{source_id}/check", response_model=SourceDetail, operation_id="checkSource")
async def check_source(
    source_id: Id, request: Request, session: SessionDep, _: Writer
) -> SourceDetail:
    """Re-run the connection and capability checks with the stored settings.

    Always returns the source; a failed check shows as `status: failing` with `last_error`.
    """
    source = await _get_or_404(session, source_id)
    try:
        password = service.stored_password(source, _cipher(request))
        result = await service.probe(
            ConnectionFields(
                engine=source.engine,
                host=source.host,
                port=source.port,
                database=source.database,
                username=source.username,
                tls_mode=source.tls_mode,
            ),
            password,
            _timeout(request),
        )
        service.record_success(source, result)
    except AdapterError as error:
        service.record_failure(source, error)
    await session.commit()
    await session.refresh(source)
    return service.to_detail(source)


@router.delete(
    "/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="deleteSource"
)
async def delete_source(source_id: Id, session: SessionDep, _: Writer) -> Response:
    source = await _get_or_404(session, source_id)
    await session.delete(source)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
