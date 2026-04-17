"""add cooldown tracking columns to companies

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("last_alerted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("companies", sa.Column("last_alerted_score", sa.Float, nullable=True))
    # Index the cooldown timestamp — we filter by it on every alert decision.
    op.create_index("ix_companies_last_alerted_at", "companies", ["last_alerted_at"])


def downgrade() -> None:
    op.drop_index("ix_companies_last_alerted_at", table_name="companies")
    op.drop_column("companies", "last_alerted_score")
    op.drop_column("companies", "last_alerted_at")
