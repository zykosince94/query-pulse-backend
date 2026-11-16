from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from query_pulse.db import Base, utcnow


class AISettings(Base):
    """The single row of AI settings (id 1). AI is off until an admin saves a provider, a
    model and a key, and turns it on."""

    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100))
    # Fernet token, like source passwords; decrypted only to call the provider.
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    # The key's last four characters, for display ("…a1b2"). The key itself is never shown.
    key_hint: Mapped[str | None] = mapped_column(String(8))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIAnswer(Base):
    """A checked answer from the provider, cached by what was asked: the evidence bundle's
    hash, the task, the provider and the model. The same question is never paid for twice."""

    __tablename__ = "ai_answers"
    __table_args__ = (
        Index("ix_ai_answers_lookup", "evidence_hash", "kind", "provider", "model"),
        Index("ix_ai_answers_finding", "finding_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # explain | draft
    evidence_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    # The validated answer (claims with their citations, and for drafts the code).
    answer: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
