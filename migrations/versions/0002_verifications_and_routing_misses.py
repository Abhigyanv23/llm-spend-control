"""Phase 4: verifications and routing_misses

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MONEY = sa.Numeric(14, 8)
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "verifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("team_id", sa.String(64), nullable=False),
        sa.Column("feature", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("routing_source", sa.String(16), nullable=False),
        sa.Column("classifier_confidence", sa.Float(), nullable=True),
        sa.Column("reference_model", sa.String(128), nullable=True),
        sa.Column("judge", sa.String(64), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("verification_cost_usd", MONEY, server_default="0", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("metadata", JSONB, nullable=False),
        sa.CheckConstraint("verdict IN ('pass', 'fail', 'inconclusive', 'skipped')",
                           name=op.f("ck_verifications_verdict_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verifications")),
        sa.UniqueConstraint("request_id", name=op.f("uq_verifications_request_id")),
    )
    op.create_index("ix_verifications_feature_created_at", "verifications",
                    ["feature", "created_at"])
    op.create_index("ix_verifications_model_created_at", "verifications",
                    ["model", "created_at"])
    op.create_index("ix_verifications_created_at", "verifications", ["created_at"])

    op.create_table(
        "routing_misses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("team_id", sa.String(64), nullable=False),
        sa.Column("feature", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("chosen_model", sa.String(128), nullable=False),
        sa.Column("chosen_tier", sa.Integer(), nullable=False),
        sa.Column("better_model", sa.String(128), nullable=False),
        sa.Column("better_tier", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("classifier_confidence", sa.Float(), nullable=True),
        sa.Column("classifier_features", JSONB, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_misses")),
        sa.UniqueConstraint("request_id", name=op.f("uq_routing_misses_request_id")),
    )
    op.create_index("ix_routing_misses_feature_created_at", "routing_misses",
                    ["feature", "created_at"])
    op.create_index("ix_routing_misses_created_at", "routing_misses", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_routing_misses_created_at", table_name="routing_misses")
    op.drop_index("ix_routing_misses_feature_created_at", table_name="routing_misses")
    op.drop_table("routing_misses")
    op.drop_index("ix_verifications_created_at", table_name="verifications")
    op.drop_index("ix_verifications_model_created_at", table_name="verifications")
    op.drop_index("ix_verifications_feature_created_at", table_name="verifications")
    op.drop_table("verifications")
