"""add target_tier + segment columns to companies

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # target_tier: 1|2|3 — Arthur's prioritization of the account.
    # segment:     "A"|"B"|"C" — from docs/icp.md §2.
    op.add_column("companies", sa.Column("target_tier", sa.Integer, nullable=False, server_default="2"))
    op.add_column("companies", sa.Column("segment", sa.String(4), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "segment")
    op.drop_column("companies", "target_tier")
