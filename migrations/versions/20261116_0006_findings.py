"""findings, index uniqueness on relation samples, rules schedule

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("target_key", sa.String(length=300), nullable=False),
        sa.Column("fingerprint_id", sa.Integer(), nullable=True),
        sa.Column("relation", sa.String(length=300), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("evidence_strength", sa.String(length=16), nullable=False),
        sa.Column("suggested_action", sa.Text(), nullable=False),
        sa.Column("action_sql", sa.Text(), nullable=True),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_changed_by", sa.String(length=16), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_findings_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fingerprint_id"],
            ["fingerprints.id"],
            name=op.f("fk_findings_fingerprint_id_fingerprints"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_findings")),
        sa.UniqueConstraint(
            "source_id", "rule_id", "target_key", name=op.f("uq_findings_source_id")
        ),
    )
    op.create_index("ix_findings_fingerprint", "findings", ["fingerprint_id"])
    with op.batch_alter_table("relation_stats") as batch:
        batch.add_column(sa.Column("is_unique", sa.Boolean(), nullable=True))
    with op.batch_alter_table("collector_states") as batch:
        batch.add_column(sa.Column("last_rules_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("collector_states") as batch:
        batch.drop_column("last_rules_at")
    with op.batch_alter_table("relation_stats") as batch:
        batch.drop_column("is_unique")
    op.drop_index("ix_findings_fingerprint", table_name="findings")
    op.drop_table("findings")
