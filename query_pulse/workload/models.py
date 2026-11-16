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
    """One normalised operation on one source: a PostgreSQL statement or a MongoDB shape.

    PostgreSQL: its own `queryid` is the fingerprint; there is no custom SQL normaliser. The
    full identity in pg_stat_statements is (dbid, userid, queryid, toplevel): the same query
    run by two roles, or nested inside a function versus run directly, is counted separately.

    MongoDB: the sha256 of the normalised query shape (workload/mongo_shape.py), with the
    collection and command beside it. The PostgreSQL columns are NULL for MongoDB rows and
    vice versa; NULLs never collide in a unique constraint, so each engine's constraint
    applies only to its own rows.
    """

    __tablename__ = "fingerprints"
    __table_args__ = (
        UniqueConstraint("source_id", "dbid", "userid", "queryid", "toplevel"),
        UniqueConstraint("source_id", "shape_hash", name="uq_fingerprints_source_id_shape_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    engine: Mapped[str] = mapped_column(String(16), default="postgres")
    dbid: Mapped[int | None] = mapped_column(BigInteger)
    userid: Mapped[int | None] = mapped_column(BigInteger)
    queryid: Mapped[int | None] = mapped_column(BigInteger)
    toplevel: Mapped[bool | None] = mapped_column(Boolean)
    shape_hash: Mapped[str | None] = mapped_column(String(64))
    namespace: Mapped[str | None] = mapped_column(String(255))  # "shop.orders"
    command: Mapped[str | None] = mapped_column(String(32))  # find, aggregate, ...
    # PostgreSQL: representative normalised text (`... WHERE id = $1`), as pg_stat_statements
    # reports it. MongoDB: the shape as JSON, every value replaced with "?".
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
    # MongoDB only (profiler): index keys and documents examined. NULL for PostgreSQL.
    keys_examined: Mapped[int | None] = mapped_column(BigInteger)
    docs_examined: Mapped[int | None] = mapped_column(BigInteger)


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
    # Indexes: UNIQUE or PRIMARY KEY (None for tables, and for samples taken before v0.5.0).
    is_unique: Mapped[bool | None] = mapped_column(Boolean)


class Plan(Base):
    """A plan captured for one fingerprint.

    PostgreSQL: EXPLAIN (GENERIC_PLAN, FORMAT JSON), as returned. MongoDB: explain at
    queryPlanner verbosity, redacted so no query value is stored (workload/mongo_plan.py),
    wrapped in a one-element list like PostgreSQL's.

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


class ServerSample(Base):
    """A point-in-time sample of a MongoDB server's serverStatus (MongoDB sources only).

    Opcounters are cumulative since the server started; rates come from comparing two
    samples, and a lower uptime than the previous sample means the server restarted.
    """

    __tablename__ = "server_samples"
    __table_args__ = (Index("ix_server_samples_source_captured", "source_id", "captured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    uptime_seconds: Mapped[float] = mapped_column(Float)
    # {"insert": n, "query": n, "update": n, "delete": n, "getmore": n, "command": n}
    opcounters: Mapped[dict[str, int]] = mapped_column(JSON)
    connections_current: Mapped[int | None] = mapped_column(Integer)
    connections_available: Mapped[int | None] = mapped_column(Integer)
    cache_bytes: Mapped[int | None] = mapped_column(BigInteger)
    cache_max_bytes: Mapped[int | None] = mapped_column(BigInteger)
    repl_state: Mapped[str | None] = mapped_column(String(32))
