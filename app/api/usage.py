"""Read the audit trail. Offset pagination (fine at this scale; keyset pagination later)."""
from datetime import datetime

from fastapi import APIRouter, Query, Request

from app.audit import UsageFilter, query_usage
from app.budgets.periods import as_utc
from app.schemas import UsagePage

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage", response_model=UsagePage)
async def get_usage(
    request: Request,
    team_id: str | None = None,
    feature: str | None = None,
    model: str | None = None,
    status: str | None = Query(default=None, description="success, provider_error, "
                               "budget_blocked, validation_error, internal_error"),
    from_: datetime | None = Query(default=None, alias="from",
                                   description="Inclusive, ISO 8601. Naive = UTC"),
    to: datetime | None = Query(default=None, description="Exclusive, ISO 8601. Naive = UTC"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    filters = UsageFilter(team_id=team_id, feature=feature, model=model, status=status,
                          start=as_utc(from_) if from_ else None,
                          end=as_utc(to) if to else None)
    return await query_usage(request.app.state.session_factory, filters, limit, offset)
