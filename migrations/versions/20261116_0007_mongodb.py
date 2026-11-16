"""MongoDB: shapes as fingerprints, examined counts, server samples, profiler cursor

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POSTGRES_IDENTITY = (
    ("dbid", sa.BigInteger()),
    ("userid", sa.BigInteger()),
    ("queryid", sa.BigInteger()),
    ("toplevel", sa.Boolean()),
)


def upgrade() -> None:
    with op.batch_alter_table("fingerprints") as batch:
        batch.add_column(
            sa.Column("engine", sa.String(length=16), nullable=False, server_default="postgres")
        )
        batch.add_column(sa.Column("shape_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("namespace", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("command", sa.String(length=32), nullable=True))
        for column, kind in _POSTGRES_IDENTITY:
            batch.alter_column(column, existing_type=kind, nullable=True)
        batch.create_unique_constraint(
            "uq_fingerprints_source_id_shape_hash", ["source_id", "shape_hash"]
        )
    with op.batch_alter_table("observations") as batch:
        batch.add_column(sa.Column("keys_examined", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("docs_examined", sa.BigInteger(), nullable=True))
    with op.batch_alter_table("collector_states") as batch:
        batch.add_column(sa.Column("profile_cursor", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("last_profile_overflow_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.create_table(
        "server_samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uptime_seconds", sa.Float(), nullable=False),
        sa.Column("opcounters", sa.JSON(), nullable=False),
        sa.Column("connections_current", sa.Integer(), nullable=True),
        sa.Column("connections_available", sa.Integer(), nullable=True),
        sa.Column("cache_bytes", sa.BigInteger(), nullable=True),
        sa.Column("cache_max_bytes", sa.BigInteger(), nullable=True),
        sa.Column("repl_state", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_server_samples_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_server_samples")),
    )
    op.create_index(
        "ix_server_samples_source_captured", "server_samples", ["source_id", "captured_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_server_samples_source_captured", table_name="server_samples")
    op.drop_table("server_samples")
    with op.batch_alter_table("collector_states") as batch:
        batch.drop_column("last_profile_overflow_at")
        batch.drop_column("profile_cursor")
    with op.batch_alter_table("observations") as batch:
        batch.drop_column("docs_examined")
        batch.drop_column("keys_examined")
    # MongoDB rows have no PostgreSQL identity, so they can't survive the NOT NULLs below.
    op.execute("DELETE FROM fingerprints WHERE engine <> 'postgres'")
    with op.batch_alter_table("fingerprints") as batch:
        batch.drop_constraint("uq_fingerprints_source_id_shape_hash", type_="unique")
        for column, kind in _POSTGRES_IDENTITY:
            batch.alter_column(column, existing_type=kind, nullable=False)
        batch.drop_column("command")
        batch.drop_column("namespace")
        batch.drop_column("shape_hash")
        batch.drop_column("engine")
