from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from query_pulse.db import Base, utcnow


class FindingStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class Finding(Base):
    """One problem a rule found on one target (an operation or an index) of one source.

    There is exactly one row per (source, rule, target), whatever its status: the unique
    constraint is the de-duplication. Re-detecting a problem updates the row instead of
    adding another, so a dismissed finding stays dismissed (QP-AC-21) and an open one is
    never duplicated (QP-AC-18).
    """

    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("source_id", "rule_id", "target_key"),
        Index("ix_findings_fingerprint", "fingerprint_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    rule_id: Mapped[str] = mapped_column(String(64))
    # What the finding is about: "operation" (a fingerprint) or "index".
    target_kind: Mapped[str] = mapped_column(String(16))
    # Stable identity of the target within the source, e.g. "operation:42" or
    # "index:public.idx_orders_legacy_lookup".
    target_key: Mapped[str] = mapped_column(String(300))
    fingerprint_id: Mapped[int | None] = mapped_column(
        ForeignKey("fingerprints.id", ondelete="CASCADE")
    )
    relation: Mapped[str | None] = mapped_column(String(300))

    # The latest detection, rewritten each time the rule fires again.
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(8))  # high | medium | low
    evidence_strength: Mapped[str] = mapped_column(String(16))  # plan | statistics
    suggested_action: Mapped[str] = mapped_column(Text)
    # SQL to review and run by hand. Never executed by Query Pulse.
    action_sql: Mapped[str | None] = mapped_column(Text)
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(16), default=FindingStatus.OPEN.value)
    # Who last changed the status: "rules" (detected, reopened, auto-resolved) or "user".
    status_changed_by: Mapped[str] = mapped_column(String(16), default="rules")
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
