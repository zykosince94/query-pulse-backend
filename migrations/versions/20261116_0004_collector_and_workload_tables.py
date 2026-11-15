"""collector and workload tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collector_heartbeat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("last_retention_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_heartbeat")),
    )
    op.create_table(
        "collector_states",
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats_reset", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dealloc", sa.BigInteger(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_eviction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evicted_statements", sa.BigInteger(), nullable=False),
        sa.Column("last_relation_stats_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_capability_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_collector_states_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_collector_states")),
    )
    op.create_table(
        "fingerprints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("dbid", sa.BigInteger(), nullable=False),
        sa.Column("userid", sa.BigInteger(), nullable=False),
        sa.Column("queryid", sa.BigInteger(), nullable=False),
        sa.Column("toplevel", sa.Boolean(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_fingerprints_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fingerprints")),
        sa.UniqueConstraint(
            "source_id",
            "dbid",
            "userid",
            "queryid",
            "toplevel",
            name=op.f("uq_fingerprints_source_id"),
        ),
    )
    op.create_table(
        "relation_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("schema_name", sa.String(length=128), nullable=False),
        sa.Column("table_name", sa.String(length=128), nullable=False),
        sa.Column("index_name", sa.String(length=128), nullable=True),
        sa.Column("seq_scan", sa.BigInteger(), nullable=True),
        sa.Column("idx_scan", sa.BigInteger(), nullable=True),
        sa.Column("live_rows", sa.BigInteger(), nullable=True),
        sa.Column("dead_rows", sa.BigInteger(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_relation_stats_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relation_stats")),
    )
    op.create_index(
        "ix_relation_stats_source_captured", "relation_stats", ["source_id", "captured_at"]
    )

    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("interval_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False),
        sa.Column("total_exec_time_ms", sa.Float(), nullable=False),
        sa.Column("mean_exec_time_ms", sa.Float(), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=False),
        sa.Column("shared_blks_hit", sa.BigInteger(), nullable=False),
        sa.Column("shared_blks_read", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["fingerprint_id"],
            ["fingerprints.id"],
            name=op.f("fk_observations_fingerprint_id_fingerprints"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_observations_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observations")),
    )
    op.create_index(
        "ix_observations_fingerprint_start", "observations", ["fingerprint_id", "interval_start"]
    )
    op.create_index("ix_observations_source_start", "observations", ["source_id", "interval_start"])


def downgrade() -> None:
    op.drop_index("ix_observations_source_start", table_name="observations")
    op.drop_index("ix_observations_fingerprint_start", table_name="observations")

    op.drop_table("observations")
    op.drop_index("ix_relation_stats_source_captured", table_name="relation_stats")

    op.drop_table("relation_stats")
    op.drop_table("fingerprints")
    op.drop_table("collector_states")
    op.drop_table("collector_heartbeat")
