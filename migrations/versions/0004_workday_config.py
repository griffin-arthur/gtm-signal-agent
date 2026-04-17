"""add workday_config JSON column to companies

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("workday_config", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "workday_config")
