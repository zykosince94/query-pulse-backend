"""API shapes for AI settings and answers. The API key goes in; it never comes out."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from query_pulse.ai.answers import ClaimOut


class ProviderOption(BaseModel):
    id: str
    label: str


class AISettingsOut(BaseModel):
    enabled: bool
    provider: str | None
    model: str | None
    key_set: bool
    # The key's last four characters ("…a1b2"), so the admin can tell which key is saved.
    key_hint: str | None
    providers: list[ProviderOption]
    # Provider requests made by this API process since it started (QP-AC-26 checks it stays 0
    # with AI off).
    provider_calls: int
    updated_at: datetime | None


class AISettingsUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=100)
    # Omit (or null) to keep the saved key. A key is only ever sent to the provider it was
    # saved for: changing the provider needs the key again.
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=500)
    enabled: bool = False

    @field_validator("model")
    @classmethod
    def _plain_model(cls, value: str) -> str:
        value = value.strip()
        if not value or any(c.isspace() or c in "/?#" for c in value):
            raise ValueError("Enter a model name only, e.g. the id shown in the provider's docs")
        return value


class RuntimeSettings(BaseModel):
    """Collection and retention settings. Read-only here: they come from the environment."""

    retention_days: int
    collector_interval_seconds: float
    rules_interval_seconds: float
    rules_window_minutes: int


class EvidenceBundleOut(BaseModel):
    """Exactly what would be sent to the provider for this finding."""

    finding_id: int
    evidence_hash: str
    items: list[dict[str, Any]]
    document: dict[str, Any]


class AnswerOut(BaseModel):
    """A checked AI answer. Advisory only: nothing in it is ever executed.

    `status` "rejected" means the server discarded the provider's answer (`error` says why,
    e.g. it cited evidence that wasn't sent). `current` is false when the finding's evidence
    has changed since the answer was given.
    """

    finding_id: int
    kind: Literal["explain", "draft"]
    status: Literal["ok", "rejected"]
    error: str | None = None
    summary: ClaimOut | None = None
    claims: list[ClaimOut] = []
    caveats: list[str] = []
    language: Literal["sql", "mongosh"] | None = None
    code: str | None = None
    provider: str
    model: str
    evidence_hash: str
    current: bool
    cached: bool
    created_at: datetime | None = None


class FindingAI(BaseModel):
    """The AI state of one finding: whether AI is on, and the latest answers given for it."""

    enabled: bool
    explain: AnswerOut | None
    draft: AnswerOut | None
