"""create data sources and capabilities

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "engine",
            sa.Enum(
                "postgres",
                "mongodb",
                name="source_engine",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database", sa.String(length=120), nullable=False),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=True),
        sa.Column("tls_mode", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("server_version", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "healthy",
                "degraded",
                "failing",
                name="source_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_sources")),
        sa.UniqueConstraint("name", name=op.f("uq_data_sources_name")),
    )
    op.create_table(
        "source_capabilities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("fix", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_source_capabilities_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_capabilities")),
        sa.UniqueConstraint("source_id", "feature", name=op.f("uq_source_capabilities_source_id")),
    )


def downgrade() -> None:
    op.drop_table("source_capabilities")
    op.drop_table("data_sources")
