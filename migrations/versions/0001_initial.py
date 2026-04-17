"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# Reference the enums but DO NOT create them when columns are added — we
# create them explicitly once at the top of upgrade().
signal_status = postgresql.ENUM(
    "pending", "validated", "rejected", "review", "suppressed",
    name="signal_status",
    create_type=False,
)
signal_tier = postgresql.ENUM(
    "tier_1", "tier_2", "tier_3",
    name="signal_tier",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        "pending", "validated", "rejected", "review", "suppressed",
        name="signal_status",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "tier_1", "tier_2", "tier_3", name="signal_tier",
    ).create(bind, checkfirst=True)

    op.create_table(
        "companies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("domain", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hubspot_id", sa.String(64), index=True),
        sa.Column("greenhouse_slug", sa.String(128)),
        sa.Column("lever_slug", sa.String(128)),
        sa.Column("is_icp", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("snoozed_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"), nullable=False, index=True),
        sa.Column("signal_type", sa.String(64), nullable=False, index=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_url", sa.String(1024), nullable=False),
        sa.Column("signal_text", sa.Text, nullable=False),
        sa.Column("raw_payload", sa.JSON, nullable=False),
        sa.Column("status", signal_status, nullable=False, server_default="pending", index=True),
        sa.Column("llm_confidence", sa.Float),
        sa.Column("llm_reasoning", sa.Text),
        sa.Column("llm_summary", sa.Text),
        sa.Column("raw_score", sa.Float, nullable=False, server_default="0"),
        sa.Column("tier", signal_tier),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "signal_type", "source_url", name="uq_signal_dedup"),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"), nullable=False, index=True),
        sa.Column("triggering_signal_id", sa.Integer, sa.ForeignKey("signals.id"), nullable=False),
        sa.Column("cumulative_score", sa.Float, nullable=False),
        sa.Column("tier", signal_tier, nullable=False),
        sa.Column("slack_channel", sa.String(128), nullable=False),
        sa.Column("slack_ts", sa.String(64)),
        sa.Column("claimed_by", sa.String(64)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("fired_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "suppressions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("pattern", sa.String(255), nullable=False),
        sa.Column("field", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "llm_cache",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cache_key", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("result_json", sa.JSON, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "circuit_breaker_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tripped_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("alert_count", sa.Integer, nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("circuit_breaker_events")
    op.drop_table("llm_cache")
    op.drop_table("suppressions")
    op.drop_table("alerts")
    op.drop_table("signals")
    op.drop_table("companies")
    postgresql.ENUM(name="signal_tier").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="signal_status").drop(op.get_bind(), checkfirst=True)
