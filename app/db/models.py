"""ORM models. Postgres is the source of truth for spend; Redis only caches counters."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Money: exact fixed-point, 14 digits total, 8 after the point (max $999,999.99999999)
Money = Numeric(14, 8, asdecimal=True)
# JSONB on Postgres (binary, indexable); plain JSON elsewhere (SQLite in unit tests)
JsonB = JSON().with_variant(JSONB(), "postgresql")

REQUEST_STATUSES = ("success", "provider_error", "budget_blocked",
                    "validation_error", "internal_error")
VERIFICATION_VERDICTS = ("pass", "fail", "inconclusive", "skipped")

# Deterministic constraint names, so Alembic migrations can refer to them reliably
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class RequestLog(Base):
    """Append-only audit trail: one row per /v1/chat request, including failures and blocks."""
    __tablename__ = "request_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)          # = request_id
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    team_id: Mapped[str] = mapped_column(String(64), nullable=False)
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Money, nullable=False, server_default="0")
    latency_ms: Mapped[float | None] = mapped_column(Float)   # a duration, not money: float is fine
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    override_reason: Mapped[str | None] = mapped_column(Text)
    # "metadata" is reserved on SQLAlchemy declarative classes, so the attribute is `meta`
    # while the column in the database is still called "metadata"
    meta: Mapped[dict] = mapped_column("metadata", JsonB, nullable=False, default=dict)
    # Phase 5 analytics columns: promoted from metadata JSON (migration 0003 backfills them)
    routed_tier: Mapped[int | None] = mapped_column(Integer)
    route_source: Mapped[str | None] = mapped_column(String(16))
    classifier_confidence: Mapped[float | None] = mapped_column(Float)
    baseline_cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    pre_escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    downgraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    prompt_fingerprint: Mapped[str | None] = mapped_column(String(64))
    prompt_preview: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(f"status IN {REQUEST_STATUSES}", name="status_valid"),
        # Composite indexes: equality column first, range column (created_at) second.
        # The team index is COVERING on Postgres (INCLUDE cost_usd): "spend by team over a
        # window" is answered from the index alone, without visiting the table rows.
        Index("ix_request_logs_team_id_created_at", "team_id", "created_at",
              postgresql_include=["cost_usd"]),
        Index("ix_request_logs_feature_created_at", "feature", "created_at"),
        Index("ix_request_logs_created_at", "created_at"),
        Index("ix_request_logs_model_created_at", "model", "created_at"),
        Index("ix_request_logs_prompt_fingerprint_created_at", "prompt_fingerprint",
              "created_at"),
    )


class BudgetPolicy(Base):
    """Daily/monthly limits for one team or one feature. NULL limit = unlimited."""
    __tablename__ = "budget_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)          # team | feature
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    daily_limit_usd: Mapped[Decimal | None] = mapped_column(Money)
    monthly_limit_usd: Mapped[Decimal | None] = mapped_column(Money)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("scope", "scope_id"),
        CheckConstraint("scope IN ('team', 'feature')", name="scope_valid"),
        CheckConstraint("daily_limit_usd IS NULL OR daily_limit_usd >= 0", name="daily_non_negative"),
        CheckConstraint("monthly_limit_usd IS NULL OR monthly_limit_usd >= 0",
                        name="monthly_non_negative"),
    )


class BudgetAlert(Base):
    """A threshold crossing, recorded once per scope + period + threshold (dedup by unique key)."""
    __tablename__ = "budget_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[str] = mapped_column(String(8), nullable=False)          # day | month
    period_key: Mapped[str] = mapped_column(String(10), nullable=False)     # 2026-09-30 | 2026-09
    threshold: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)  # 0.80, 1.00
    projected_usd: Mapped[Decimal] = mapped_column(Money, nullable=False)
    limit_usd: Mapped[Decimal] = mapped_column(Money, nullable=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "period", "period_key", "threshold"),
    )


class Verification(Base):
    """Result of verifying one sampled response against a stronger reference model (Phase 4).
    request_id is UNIQUE: the queue delivers at-least-once, so a re-delivered job must not
    insert a second row (idempotency key)."""
    __tablename__ = "verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    team_id: Mapped[str] = mapped_column(String(64), nullable=False)
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)          # the cheap model
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    routing_source: Mapped[str] = mapped_column(String(16), nullable=False)
    classifier_confidence: Mapped[float | None] = mapped_column(Float)
    reference_model: Mapped[str | None] = mapped_column(String(128))
    judge: Mapped[str | None] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    verification_cost_usd: Mapped[Decimal] = mapped_column(Money, nullable=False,
                                                           server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    meta: Mapped[dict] = mapped_column("metadata", JsonB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("request_id"),
        CheckConstraint(f"verdict IN {VERIFICATION_VERDICTS}", name="verdict_valid"),
        Index("ix_verifications_feature_created_at", "feature", "created_at"),
        Index("ix_verifications_model_created_at", "model", "created_at"),
        Index("ix_verifications_created_at", "created_at"),
    )


class RoutingMiss(Base):
    """A cheap answer that failed verification: a labelled example of 'this request needed a
    stronger tier'. Training data for a future learned classifier."""
    __tablename__ = "routing_misses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    team_id: Mapped[str] = mapped_column(String(64), nullable=False)
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text)        # NULL when privacy.store_prompts=false
    chosen_model: Mapped[str] = mapped_column(String(128), nullable=False)
    chosen_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    better_model: Mapped[str] = mapped_column(String(128), nullable=False)
    better_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    classifier_confidence: Mapped[float | None] = mapped_column(Float)
    classifier_features: Mapped[dict | None] = mapped_column(JsonB)

    __table_args__ = (
        UniqueConstraint("request_id"),
        Index("ix_routing_misses_feature_created_at", "feature", "created_at"),
        Index("ix_routing_misses_created_at", "created_at"),
    )
