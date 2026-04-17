"""digest table for batched alerts

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digest_items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("alert_id", sa.Integer, sa.ForeignKey("alerts.id"), nullable=False, unique=True),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("flushed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("digest_items")
