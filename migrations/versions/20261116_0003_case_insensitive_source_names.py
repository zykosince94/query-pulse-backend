"""case-insensitive unique source names

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch mode lets SQLite (tests) drop a constraint by rebuilding the table.
    with op.batch_alter_table("data_sources") as batch:
        batch.drop_constraint("uq_data_sources_name", type_="unique")
    op.create_index(
        "uq_data_sources_lower_name",
        "data_sources",
        [sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_data_sources_lower_name", table_name="data_sources")
    with op.batch_alter_table("data_sources") as batch:
        batch.create_unique_constraint("uq_data_sources_name", ["name"])
