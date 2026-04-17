"""add ashby_slug + ticker columns to companies

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("ashby_slug", sa.String(128), nullable=True))
    op.add_column("companies", sa.Column("ticker", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "ticker")
    op.drop_column("companies", "ashby_slug")
