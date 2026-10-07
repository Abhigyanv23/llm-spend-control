"""Phase 2: request_logs audit trail, budget_policies, budget_alerts

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 8)
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "request_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("team_id", sa.String(64), nullable=False),
        sa.Column("feature", sa.String(64), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost_usd", MONEY, server_default="0", nullable=False),
        sa.Column("cost_usd", MONEY, server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("metadata", JSONB, nullable=False),
        sa.CheckConstraint(
            "status IN ('success', 'provider_error', 'budget_blocked', "
            "'validation_error', 'internal_error')",
            name=op.f("ck_request_logs_status_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_logs")),
    )
    op.create_index("ix_request_logs_team_id_created_at", "request_logs",
                    ["team_id", "created_at"])
    op.create_index("ix_request_logs_feature_created_at", "request_logs",
                    ["feature", "created_at"])
    op.create_index("ix_request_logs_created_at", "request_logs", ["created_at"])

    op.create_table(
        "budget_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("daily_limit_usd", MONEY, nullable=True),
        sa.Column("monthly_limit_usd", MONEY, nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.CheckConstraint("scope IN ('team', 'feature')",
                           name=op.f("ck_budget_policies_scope_valid")),
        sa.CheckConstraint("daily_limit_usd IS NULL OR daily_limit_usd >= 0",
                           name=op.f("ck_budget_policies_daily_non_negative")),
        sa.CheckConstraint("monthly_limit_usd IS NULL OR monthly_limit_usd >= 0",
                           name=op.f("ck_budget_policies_monthly_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_policies")),
        sa.UniqueConstraint("scope", "scope_id", name=op.f("uq_budget_policies_scope_scope_id")),
    )

    op.create_table(
        "budget_alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("period", sa.String(8), nullable=False),
        sa.Column("period_key", sa.String(10), nullable=False),
        sa.Column("threshold", sa.Numeric(4, 2), nullable=False),
        sa.Column("projected_usd", MONEY, nullable=False),
        sa.Column("limit_usd", MONEY, nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_alerts")),
        sa.UniqueConstraint("scope", "scope_id", "period", "period_key", "threshold",
                            name=op.f("uq_budget_alerts_scope_scope_id_period_period_key_threshold")),
    )


def downgrade() -> None:
    op.drop_table("budget_alerts")
    op.drop_table("budget_policies")
    op.drop_index("ix_request_logs_created_at", table_name="request_logs")
    op.drop_index("ix_request_logs_feature_created_at", table_name="request_logs")
    op.drop_index("ix_request_logs_team_id_created_at", table_name="request_logs")
    op.drop_table("request_logs")
