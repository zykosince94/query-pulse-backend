from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from query_pulse.db import Base


class CollectorState(Base):
    """Everything the collector must remember about a source between cycles, and restarts.

    The last raw snapshot is the baseline for the next delta; keeping it here (not in memory)
    is what makes a collector restart produce a correct first interval (QP-AC-11).
    """

    __tablename__ = "collector_states"

    source_id: Mapped[int] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True
    )
    # Serialised delta.Snapshot (see collector/delta.py); None until the first success.
    # none_as_null: "no baseline" is SQL NULL, not the JSON value `null`.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats_reset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dealloc: Mapped[int | None] = mapped_column(BigInteger)

    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    # Evictions from pg_stat_statements (its `dealloc` counter rose): results are incomplete.
    last_eviction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evicted_statements: Mapped[int] = mapped_column(BigInteger, default=0)

    last_relation_stats_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_capability_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last full re-capture of the top operations' plans (new operations are planned sooner).
    last_plan_capture_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectorHeartbeat(Base):
    """A single row the running collector updates, so the API can tell whether it's alive."""

    __tablename__ = "collector_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    host: Mapped[str] = mapped_column(String(255))
    last_retention_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
