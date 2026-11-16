from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from query_pulse.db import Base, utcnow


class Fingerprint(Base):
    """One normalised statement on one source.

    PostgreSQL's own `queryid` is the fingerprint; there is no custom SQL normaliser. The full
    identity in pg_stat_statements is (dbid, userid, queryid, toplevel): the same query run by
    two roles, or nested inside a function versus run directly, is counted separately.
    """

    __tablename__ = "fingerprints"
    __table_args__ = (UniqueConstraint("source_id", "dbid", "userid", "queryid", "toplevel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    dbid: Mapped[int] = mapped_column(BigInteger)
    userid: Mapped[int] = mapped_column(BigInteger)
    queryid: Mapped[int] = mapped_column(BigInteger)
    toplevel: Mapped[bool] = mapped_column(Boolean)
    # Representative normalised text (`... WHERE id = $1`), as pg_stat_statements reports it.
    query: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Observation(Base):
    """What one fingerprint did during one collection interval: deltas, never totals."""

    __tablename__ = "observations"
    __table_args__ = (
        Index("ix_observations_fingerprint_start", "fingerprint_id", "interval_start"),
        Index("ix_observations_source_start", "source_id", "interval_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint_id: Mapped[int] = mapped_column(ForeignKey("fingerprints.id", ondelete="CASCADE"))
    # Denormalised so per-source time ranges don't need a join.
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    calls: Mapped[int] = mapped_column(BigInteger)
    total_exec_time_ms: Mapped[float] = mapped_column(Float)
    mean_exec_time_ms: Mapped[float] = mapped_column(Float)
    rows: Mapped[int] = mapped_column(BigInteger)
    shared_blks_hit: Mapped[int] = mapped_column(BigInteger)
    shared_blks_read: Mapped[int] = mapped_column(BigInteger)


class RelationStat(Base):
    """A point-in-time sample of one table or index (pg_stat_user_tables / _indexes).

    These counters are cumulative too; Phase 5's rules compare samples across a window.
    """

    __tablename__ = "relation_stats"
    __table_args__ = (Index("ix_relation_stats_source_captured", "source_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(8))  # "table" or "index"
    schema_name: Mapped[str] = mapped_column(String(128))
    table_name: Mapped[str] = mapped_column(String(128))
    index_name: Mapped[str | None] = mapped_column(String(128))
    seq_scan: Mapped[int | None] = mapped_column(BigInteger)
    idx_scan: Mapped[int | None] = mapped_column(BigInteger)
    live_rows: Mapped[int | None] = mapped_column(BigInteger)
    dead_rows: Mapped[int | None] = mapped_column(BigInteger)
    size_bytes: Mapped[int] = mapped_column(BigInteger)


class Plan(Base):
    """A generic plan captured for one fingerprint: EXPLAIN (GENERIC_PLAN, FORMAT JSON).

    Rows are immutable evidence (Phase 5 findings cite them by id), so a new row is written
    only when the plan changes; re-checking an unchanged plan just moves `checked_at`. A
    capture that failed is stored too, with `error` set and `plan` empty, so the UI can say
    why there's no plan instead of showing nothing.
    """

    __tablename__ = "plans"
    __table_args__ = (Index("ix_plans_fingerprint_captured", "fingerprint_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint_id: Mapped[int] = mapped_column(ForeignKey("fingerprints.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    engine: Mapped[str] = mapped_column(String(16))
    # none_as_null: "no plan" is SQL NULL, not the JSON value `null`.
    plan: Mapped[list[Any] | None] = mapped_column(JSON(none_as_null=True))
    error: Mapped[str | None] = mapped_column(Text)
    # sha256 of the plan JSON (or of the error), to tell "same plan again" from "changed".
    content_hash: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
