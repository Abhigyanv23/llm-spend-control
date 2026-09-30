"""Audit trail: write one request_logs row per request, and query them for /v1/usage."""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import RequestLog
from app.money import quantize_usd

logger = logging.getLogger(__name__)


@dataclass
class AuditRecord:
    """Everything we know about one request, filled in as it moves through the pipeline."""
    request_id: str
    created_at: datetime
    team_id: str
    feature: str
    priority: str
    status: str = "success"
    model: str | None = None
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal(0)
    cost_usd: Decimal = Decimal(0)
    latency_ms: float | None = None
    error_code: str | None = None
    override_reason: str | None = None
    metadata: dict = field(default_factory=dict)    # must be JSON-serialisable (no Decimal)


class AuditLogger:
    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    async def write(self, record: AuditRecord) -> None:
        """Insert one row. Runs as a background task, so it must NEVER raise: there is
        no client left to receive the error. Failures are logged loudly instead."""
        try:
            async with self.session_factory() as session:
                session.add(RequestLog(
                    id=uuid.UUID(record.request_id), created_at=record.created_at,
                    team_id=record.team_id, feature=record.feature, priority=record.priority,
                    model=record.model, provider=record.provider,
                    input_tokens=record.input_tokens, output_tokens=record.output_tokens,
                    estimated_cost_usd=record.estimated_cost_usd, cost_usd=record.cost_usd,
                    latency_ms=record.latency_ms, status=record.status,
                    error_code=record.error_code, override_reason=record.override_reason,
                    meta=record.metadata))
                await session.commit()
        except Exception:
            logger.exception("AUDIT WRITE FAILED request_id=%s team=%s cost=%s",
                             record.request_id, record.team_id, record.cost_usd)


# ------------------------------------------------------------------ read side (/v1/usage)

@dataclass
class UsageFilter:
    team_id: str | None = None
    feature: str | None = None
    model: str | None = None
    status: str | None = None
    start: datetime | None = None       # inclusive
    end: datetime | None = None         # exclusive

    def conditions(self) -> list:
        conds = []
        for column, value in ((RequestLog.team_id, self.team_id),
                              (RequestLog.feature, self.feature),
                              (RequestLog.model, self.model),
                              (RequestLog.status, self.status)):
            if value is not None:
                conds.append(column == value)
        if self.start is not None:
            conds.append(RequestLog.created_at >= self.start)
        if self.end is not None:
            conds.append(RequestLog.created_at < self.end)
        return conds


async def query_usage(session_factory: async_sessionmaker, filters: UsageFilter,
                      limit: int, offset: int) -> dict:
    conds = filters.conditions()
    async with session_factory() as session:
        totals_row = (await session.execute(
            select(func.count(),
                   func.coalesce(func.sum(RequestLog.cost_usd), 0),
                   func.coalesce(func.sum(RequestLog.input_tokens), 0),
                   func.coalesce(func.sum(RequestLog.output_tokens), 0))
            .where(*conds))).one()
        rows = (await session.scalars(
            select(RequestLog).where(*conds)
            .order_by(RequestLog.created_at.desc(), RequestLog.id)
            .limit(limit).offset(offset))).all()

    count, cost, tokens_in, tokens_out = totals_row
    items = [{"request_id": str(r.id), "created_at": r.created_at, "team_id": r.team_id,
              "feature": r.feature, "priority": r.priority, "model": r.model,
              "provider": r.provider, "input_tokens": r.input_tokens,
              "output_tokens": r.output_tokens, "estimated_cost_usd": r.estimated_cost_usd,
              "cost_usd": r.cost_usd, "latency_ms": r.latency_ms, "status": r.status,
              "error_code": r.error_code, "override_reason": r.override_reason,
              "metadata": r.meta} for r in rows]
    return {"items": items,
            "totals": {"count": count, "cost_usd": quantize_usd(cost),
                       "input_tokens": int(tokens_in), "output_tokens": int(tokens_out)},
            "limit": limit, "offset": offset, "has_more": offset + len(items) < count}
