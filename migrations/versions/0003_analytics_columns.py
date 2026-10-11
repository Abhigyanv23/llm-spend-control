"""Phase 5: promote analytics fields from request_logs.metadata to columns + backfill

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02

Why: dashboard queries aggregate millions of rows by tier, source, escalation and baseline
cost. Reading those out of JSONB on every query is slow (no plain B-tree index, a cast per row)
and dialect-specific. Real columns are fast, indexable and portable. The JSON stays as the
complete record; the columns are a denormalised copy for reads.
"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(14, 8)

# One set-based statement: Postgres JSON operators (-> object, ->> text) + casts.
# Rows without routing metadata (validation errors) keep NULLs/false, which is correct.
POSTGRES_BACKFILL = """
UPDATE request_logs SET
    route_source          = metadata -> 'routing' ->> 'source',
    routed_tier           = (metadata -> 'routing' ->> 'final_tier')::int,
    classifier_confidence = (metadata -> 'routing' -> 'classifier' ->> 'confidence')::float,
    baseline_cost_usd     = (metadata -> 'routing' ->> 'baseline_cost_usd')::numeric(14, 8),
    escalated     = COALESCE((metadata -> 'escalation' ->> 'escalated')::boolean, false),
    pre_escalated = COALESCE((metadata -> 'escalation' -> 'pre_call' ->> 'applied')::boolean,
                             false),
    downgraded    = COALESCE(jsonb_array_length(
                        CASE WHEN jsonb_typeof(metadata -> 'routing' -> 'downgrades') = 'array'
                             THEN metadata -> 'routing' -> 'downgrades' END), 0) > 0
WHERE jsonb_typeof(metadata -> 'routing') = 'object'
"""


def _row_values(meta: dict) -> dict:
    """The same extraction in Python, for databases without Postgres JSON operators."""
    routing = meta.get("routing") if isinstance(meta.get("routing"), dict) else {}
    escalation = meta.get("escalation") if isinstance(meta.get("escalation"), dict) else {}
    pre_call = escalation.get("pre_call") if isinstance(escalation.get("pre_call"), dict) else {}
    classifier = routing.get("classifier") if isinstance(routing.get("classifier"), dict) else {}
    return {"route_source": routing.get("source"), "routed_tier": routing.get("final_tier"),
            "classifier_confidence": classifier.get("confidence"),
            "baseline_cost_usd": routing.get("baseline_cost_usd"),
            "escalated": bool(escalation.get("escalated")),
            "pre_escalated": bool(pre_call.get("applied")),
            "downgraded": bool(routing.get("downgrades"))}


def _backfill_portable(bind) -> None:
    rows = bind.execute(sa.text("SELECT id, metadata FROM request_logs")).fetchall()
    update = sa.text(
        "UPDATE request_logs SET route_source = :route_source, routed_tier = :routed_tier, "
        "classifier_confidence = :classifier_confidence, baseline_cost_usd = :baseline_cost_usd, "
        "escalated = :escalated, pre_escalated = :pre_escalated, downgraded = :downgraded "
        "WHERE id = :id")
    for row_id, raw in rows:
        meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        bind.execute(update, {"id": row_id, **_row_values(meta)})


def upgrade() -> None:
    with op.batch_alter_table("request_logs") as batch:       # batch: SQLite-compatible ALTER
        batch.add_column(sa.Column("routed_tier", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("route_source", sa.String(16), nullable=True))
        batch.add_column(sa.Column("classifier_confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("baseline_cost_usd", MONEY, nullable=True))
        batch.add_column(sa.Column("escalated", sa.Boolean(), server_default=sa.false(),
                                   nullable=False))
        batch.add_column(sa.Column("pre_escalated", sa.Boolean(), server_default=sa.false(),
                                   nullable=False))
        batch.add_column(sa.Column("downgraded", sa.Boolean(), server_default=sa.false(),
                                   nullable=False))
        batch.add_column(sa.Column("prompt_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("prompt_preview", sa.Text(), nullable=True))

    # Backfill BEFORE creating the new indexes (cheaper: indexes aren't updated row by row).
    # Old rows get no fingerprint/preview: the prompt was never stored, so it can't be derived.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(POSTGRES_BACKFILL)
    else:
        _backfill_portable(bind)

    op.create_index("ix_request_logs_model_created_at", "request_logs", ["model", "created_at"])
    op.create_index("ix_request_logs_prompt_fingerprint_created_at", "request_logs",
                    ["prompt_fingerprint", "created_at"])
    # Upgrade the team index to a covering index on Postgres (INCLUDE is ignored elsewhere)
    op.drop_index("ix_request_logs_team_id_created_at", table_name="request_logs")
    op.create_index("ix_request_logs_team_id_created_at", "request_logs",
                    ["team_id", "created_at"], postgresql_include=["cost_usd"])


def downgrade() -> None:
    op.drop_index("ix_request_logs_team_id_created_at", table_name="request_logs")
    op.create_index("ix_request_logs_team_id_created_at", "request_logs",
                    ["team_id", "created_at"])
    op.drop_index("ix_request_logs_prompt_fingerprint_created_at", table_name="request_logs")
    op.drop_index("ix_request_logs_model_created_at", table_name="request_logs")
    with op.batch_alter_table("request_logs") as batch:
        for column in ("prompt_preview", "prompt_fingerprint", "downgraded", "pre_escalated",
                       "escalated", "baseline_cost_usd", "classifier_confidence",
                       "route_source", "routed_tier"):
            batch.drop_column(column)
