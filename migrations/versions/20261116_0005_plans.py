"""plans

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("engine", sa.String(length=16), nullable=False),
        sa.Column("plan", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["fingerprint_id"],
            ["fingerprints.id"],
            name=op.f("fk_plans_fingerprint_id_fingerprints"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.id"],
            name=op.f("fk_plans_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
    )
    op.create_index("ix_plans_fingerprint_captured", "plans", ["fingerprint_id", "captured_at"])
    with op.batch_alter_table("collector_states") as batch:
        batch.add_column(
            sa.Column("last_plan_capture_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("collector_states") as batch:
        batch.drop_column("last_plan_capture_at")
    op.drop_index("ix_plans_fingerprint_captured", table_name="plans")
    op.drop_table("plans")
