from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from query_pulse.collector.models import CollectorState
from query_pulse.db import Base, TimestampMixin, utcnow


class SourceEngine(enum.StrEnum):
    POSTGRES = "postgres"
    MONGODB = "mongodb"


class SourceStatus(enum.StrEnum):
    HEALTHY = "healthy"  # last check connected and every capability is available
    DEGRADED = "degraded"  # connected, but some capabilities are missing (see their fixes)
    FAILING = "failing"  # last check couldn't connect (see last_error)


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    # Stored as VARCHAR + CHECK (not a native PG enum), so adding a value later is a simple
    # migration and SQLite tests behave the same.
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda members: [m.value for m in members],
    )


class DataSource(TimestampMixin, Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Unique ignoring case: see the functional index below.
    name: Mapped[str] = mapped_column(String(120))
    engine: Mapped[SourceEngine] = mapped_column(_enum(SourceEngine, "source_engine"))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    database: Mapped[str] = mapped_column(String(120))
    username: Mapped[str] = mapped_column(String(120), default="")
    # Fernet token; decrypted only in the service layer, right before an adapter connects.
    password_encrypted: Mapped[str | None] = mapped_column(Text, default=None)
    tls_mode: Mapped[str] = mapped_column(String(16), default="disable")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(500), default="")

    server_version: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[SourceStatus] = mapped_column(_enum(SourceStatus, "source_status"))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    capabilities: Mapped[list[SourceCapability]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        order_by="SourceCapability.position",
        lazy="selectin",
    )
    # Written by the collector; read-only from here. The database deletes it with the source.
    collector_state: Mapped[CollectorState | None] = relationship(
        CollectorState,
        primaryjoin="DataSource.id == foreign(CollectorState.source_id)",
        lazy="selectin",
        viewonly=True,
        uselist=False,
    )


# "Orders" and "orders" are the same name. A functional unique index enforces it in the
# database, so two concurrent requests can't both pass the application-level check.
Index("uq_data_sources_lower_name", func.lower(DataSource.name), unique=True)


class SourceCapability(Base):
    __tablename__ = "source_capabilities"
    __table_args__ = (UniqueConstraint("source_id", "feature"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    feature: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(120))
    available: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(Text)
    fix: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    source: Mapped[DataSource] = relationship(back_populates="capabilities")
