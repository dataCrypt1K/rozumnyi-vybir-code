"""Separate hosted projects from the unassigned local records.

Revision ID: 20261122_0004
Revises: 20261009_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20261122_0004"
down_revision = "20261009_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assessments", sa.Column("owner_key", sa.String(128), nullable=True))
    op.create_index(
        "ix_assessments_owner_updated_at",
        "assessments",
        ["owner_key", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_assessments_owner_updated_at", table_name="assessments")
    op.drop_column("assessments", "owner_key")
