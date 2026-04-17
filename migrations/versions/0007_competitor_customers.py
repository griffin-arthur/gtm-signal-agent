"""competitor_customers cache table

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "competitor_customers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                  nullable=False, index=True),
        sa.Column("competitor", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("evidence_url", sa.String(1024), nullable=False),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True),
                  nullable=False, index=True),
        sa.Column("is_override", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "competitor",
                            name="uq_competitor_customer_company_comp"),
    )


def downgrade() -> None:
    op.drop_table("competitor_customers")
