"""AI settings, the evidence bundle, and the explain / draft endpoints.

AI is off by default. An admin turns it on by saving a provider, a model and a key; the key is
checked with the provider before AI is enabled. Asking costs money, so only the admin can ask;
everyone can read the answers already given and the bundle that would be sent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from query_pulse.accounts.dependencies import SessionDep, Writer, current_user
from query_pulse.ai.answers import SCHEMAS, SYSTEM, Checked, Kind, check, prompt
from query_pulse.ai.evidence import build_bundle
from query_pulse.ai.models import AIAnswer, AISettings
from query_pulse.ai.provider import (
    FAKE,
    PROVIDERS,
    AIProvider,
    FakeProvider,
    OpenAIProvider,
    ProviderError,
)
from query_pulse.ai.schemas import (
    AISettingsOut,
    AISettingsUpdate,
    AnswerOut,
    EvidenceBundleOut,
    FindingAI,
    ProviderOption,
    RuntimeSettings,
)
from query_pulse.api_types import Id
from query_pulse.config import Settings
from query_pulse.crypto import CredentialCipher
from query_pulse.db import utcnow
from query_pulse.findings.models import Finding
from query_pulse.workload.service import as_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["ai"], dependencies=[Depends(current_user)])

ProviderFactory = Callable[[str, str], AIProvider]

RETARGET = (
    "Enter the API key again: the provider changed, and a saved key is only ever sent to the "
    "provider it was saved for."
)


def default_factory(settings: Settings) -> ProviderFactory:
    def build(provider: str, api_key: str) -> AIProvider:
        if provider == "openai":
            return OpenAIProvider(api_key, settings.openai_base_url, settings.ai_timeout_seconds)
        if provider == FAKE and settings.ai_allow_fake_provider:
            return FakeProvider()
        raise ProviderError(f"Unknown AI provider '{provider}'.")

    return build


class CountingProvider:
    """Counts every request to the provider (QP-AC-26: none at all while AI is off)."""

    def __init__(self, inner: AIProvider, request: Request) -> None:
        self.inner = inner
        self.name = inner.name
        self.state = request.app.state

    def _count(self) -> None:
        self.state.ai_provider_calls = getattr(self.state, "ai_provider_calls", 0) + 1

    async def check(self, model: str) -> None:
        self._count()
        await self.inner.check(model)

    async def complete(self, **kwargs: object) -> dict[str, object]:
        self._count()
        return await self.inner.complete(**kwargs)  # type: ignore[arg-type]


def _provider(request: Request, name: str, api_key: str) -> CountingProvider:
    factory: ProviderFactory = request.app.state.ai_provider_factory
    return CountingProvider(factory(name, api_key), request)


def _allowed(settings: Settings) -> dict[str, str]:
    allowed = dict(PROVIDERS)
    if settings.ai_allow_fake_provider:
        allowed[FAKE] = "Fake (tests only)"
    return allowed


async def _row(session: AsyncSession) -> AISettings:
    row = await session.get(AISettings, 1)
    if row is None:
        row = AISettings(id=1, enabled=False, updated_at=utcnow())
        session.add(row)
        await session.flush()
    return row


def _out(row: AISettings, request: Request) -> AISettingsOut:
    settings: Settings = request.app.state.settings
    return AISettingsOut(
        enabled=row.enabled,
        provider=row.provider,
        model=row.model,
        key_set=bool(row.api_key_encrypted),
        key_hint=f"…{row.key_hint}" if row.key_hint else None,
        providers=[ProviderOption(id=k, label=v) for k, v in _allowed(settings).items()],
        provider_calls=getattr(request.app.state, "ai_provider_calls", 0),
        updated_at=as_utc(row.updated_at) if row.updated_at else None,
    )


@router.get("/ai/settings", response_model=AISettingsOut, operation_id="getAISettings")
async def get_ai_settings(request: Request, session: SessionDep) -> AISettingsOut:
    """Whether AI is on, with which provider and model. The key is never returned."""
    row = await session.get(AISettings, 1)
    return _out(row or AISettings(id=1, enabled=False), request)


@router.put(
    "/ai/settings",
    response_model=AISettingsOut,
    operation_id="updateAISettings",
    responses={400: {"description": "The key or model was refused; nothing was saved"}},
)
async def update_ai_settings(
    payload: AISettingsUpdate, request: Request, session: SessionDep, _: Writer
) -> AISettingsOut:
    """Save the provider, model and (optionally) a new key. Turning AI on checks the key and
    model with the provider first; if that fails, nothing is saved."""
    settings: Settings = request.app.state.settings
    cipher: CredentialCipher = request.app.state.cipher
    if payload.provider not in _allowed(settings):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unknown AI provider")
    row = await _row(session)
    new_key = payload.api_key.get_secret_value() if payload.api_key is not None else None
    if new_key is None and row.api_key_encrypted and row.provider != payload.provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=RETARGET)
    key = new_key or (cipher.decrypt(row.api_key_encrypted) if row.api_key_encrypted else None)
    if payload.enabled:
        if not key:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="Save an API key to turn AI on."
            )
        try:
            await _provider(request, payload.provider, key).check(payload.model)
        except ProviderError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    row.provider = payload.provider
    row.model = payload.model
    row.enabled = payload.enabled
    if new_key is not None:
        row.api_key_encrypted = cipher.encrypt(new_key)
        row.key_hint = new_key[-4:]
    row.updated_at = utcnow()
    await session.commit()
    logger.info(
        "ai settings saved: provider=%s model=%s enabled=%s", row.provider, row.model, row.enabled
    )
    return _out(row, request)


@router.delete("/ai/settings/key", response_model=AISettingsOut, operation_id="deleteAIKey")
async def delete_ai_key(request: Request, session: SessionDep, _: Writer) -> AISettingsOut:
    """Delete the saved key. AI turns off."""
    row = await _row(session)
    row.api_key_encrypted = None
    row.key_hint = None
    row.enabled = False
    row.updated_at = utcnow()
    await session.commit()
    logger.info("ai key deleted")
    return _out(row, request)


@router.get("/settings", response_model=RuntimeSettings, operation_id="getRuntimeSettings")
async def runtime_settings(request: Request) -> RuntimeSettings:
    """Collection and retention settings (from the environment; read-only)."""
    settings: Settings = request.app.state.settings
    return RuntimeSettings(
        retention_days=settings.retention_days,
        collector_interval_seconds=settings.collector_interval_seconds,
        rules_interval_seconds=settings.rules_interval_seconds,
        rules_window_minutes=settings.rules_window_minutes,
    )


# --- per finding ----------------------------------------------------------------------------

FindingId = Id


async def _finding(session: AsyncSession, finding_id: int) -> Finding:
    finding = await session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Finding not found")
    return finding


def _answer(row: AIAnswer, current_hash: str, cached: bool) -> AnswerOut:
    checked = Checked.model_validate(row.answer)
    return AnswerOut(
        finding_id=row.finding_id,
        kind=row.kind,
        status=checked.status,
        error=checked.error,
        summary=checked.summary,
        claims=checked.claims,
        caveats=checked.caveats,
        language=checked.language,
        code=checked.code,
        provider=row.provider,
        model=row.model,
        evidence_hash=row.evidence_hash,
        current=row.evidence_hash == current_hash,
        cached=cached,
        created_at=as_utc(row.created_at),
    )


@router.get(
    "/findings/{finding_id}/ai/bundle",
    response_model=EvidenceBundleOut,
    operation_id="getFindingEvidenceBundle",
)
async def finding_bundle(finding_id: FindingId, session: SessionDep) -> EvidenceBundleOut:
    """Exactly what would be sent to the AI provider for this finding ("view what was sent")."""
    bundle = await build_bundle(session, await _finding(session, finding_id))
    return EvidenceBundleOut(
        finding_id=finding_id,
        evidence_hash=bundle.hash,
        items=bundle.document["items"],
        document=bundle.document,
    )


@router.get("/findings/{finding_id}/ai", response_model=FindingAI, operation_id="getFindingAI")
async def finding_ai(finding_id: FindingId, session: SessionDep) -> FindingAI:
    """Whether AI is on, and the latest explanation and draft given for this finding."""
    finding = await _finding(session, finding_id)
    row = await session.get(AISettings, 1)
    current = (await build_bundle(session, finding)).hash
    latest: dict[str, AnswerOut | None] = {"explain": None, "draft": None}
    for kind in latest:
        answer = await session.scalar(
            select(AIAnswer)
            .where(AIAnswer.finding_id == finding_id, AIAnswer.kind == kind)
            .order_by(AIAnswer.id.desc())
            .limit(1)
        )
        if answer is not None:
            latest[kind] = _answer(answer, current, cached=True)
    return FindingAI(
        enabled=bool(row and row.enabled), explain=latest["explain"], draft=latest["draft"]
    )


@router.post(
    "/findings/{finding_id}/ai/{kind}",
    response_model=AnswerOut,
    operation_id="askFindingAI",
    responses={
        409: {"description": "AI is off"},
        502: {"description": "The AI provider couldn't answer"},
    },
)
async def ask_finding_ai(
    finding_id: FindingId, kind: Kind, request: Request, session: SessionDep, _: Writer
) -> AnswerOut:
    """Explain the finding, or draft a fix, with the configured provider.

    The answer is checked before it's returned: citing evidence that wasn't sent, or drafting a
    destructive statement, rejects it. Good answers are cached by the evidence they were given,
    so asking the same question again costs nothing.
    """
    finding = await _finding(session, finding_id)
    row = await session.get(AISettings, 1)
    if row is None or not row.enabled or not row.provider or not row.model:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="AI is off. An admin can turn it on in Settings."
        )
    bundle = await build_bundle(session, finding)
    cached = await session.scalar(
        select(AIAnswer)
        .where(
            AIAnswer.evidence_hash == bundle.hash,
            AIAnswer.kind == kind,
            AIAnswer.provider == row.provider,
            AIAnswer.model == row.model,
        )
        .order_by(AIAnswer.id.desc())
        .limit(1)
    )
    if cached is not None:
        return _answer(cached, bundle.hash, cached=True)

    cipher: CredentialCipher = request.app.state.cipher
    key = cipher.decrypt(row.api_key_encrypted) if row.api_key_encrypted else ""
    schema_name, schema = SCHEMAS[kind]
    try:
        raw = await _provider(request, row.provider, key).complete(
            model=row.model,
            system=SYSTEM,
            user=prompt(kind, bundle),
            schema_name=schema_name,
            schema=schema,
        )
    except ProviderError as exc:
        logger.warning("ai request failed: finding_id=%s kind=%s", finding_id, kind)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    checked = check(kind, raw, bundle)
    logger.info(
        "ai answer: finding_id=%s kind=%s status=%s provider=%s model=%s",
        finding_id,
        kind,
        checked.status,
        row.provider,
        row.model,
    )
    answer = AIAnswer(
        finding_id=finding_id,
        kind=kind,
        evidence_hash=bundle.hash,
        provider=row.provider,
        model=row.model,
        answer=checked.model_dump(),
        created_at=utcnow(),
    )
    if checked.status == "ok":
        session.add(answer)  # rejected answers aren't cached: asking again may do better
        await session.commit()
    return _answer(answer, bundle.hash, cached=False)
