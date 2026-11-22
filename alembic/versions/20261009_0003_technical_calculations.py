"""Add versioned technical channel calculations.

Revision ID: 20261009_0003
Revises: 20261009_0002
Create Date: 2026-10-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261009_0003"
down_revision: str | None = "20261009_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "technical_calculations",
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("rules_version", sa.String(length=80), nullable=False),
        sa.Column("reserve_ratio", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_id"], ["assessments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("assessment_id"),
    )


def downgrade() -> None:
    op.drop_table("technical_calculations")
