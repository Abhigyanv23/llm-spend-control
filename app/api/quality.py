"""Quality API: how safe is cheap routing, what do misses look like, is the queue healthy.
No auth yet (known limitation): prompts in routing misses are visible to any caller."""
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request
from redis.exceptions import RedisError

from app.budgets.periods import as_utc
from app.errors import GatewayError
from app.quality.reports import QualityFilter, list_misses, quality_summary

router = APIRouter(prefix="/v1/quality", tags=["quality"])

DEFAULT_WINDOW = timedelta(days=7)


def _window(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime | None]:
    start = as_utc(from_) if from_ else datetime.now(UTC) - DEFAULT_WINDOW
    return start, as_utc(to) if to else None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


@router.get("")
async def get_quality(
    request: Request,
    team_id: str | None = None,
    feature: str | None = None,
    from_: datetime | None = Query(default=None, alias="from",
                                   description="Inclusive, ISO 8601 (default: 7 days ago)"),
    to: datetime | None = Query(default=None, description="Exclusive, ISO 8601 (default: now)"),
):
    start, end = _window(from_, to)
    f = QualityFilter(start=start, end=end, team_id=team_id, feature=feature)
    summary = await quality_summary(request.app.state.session_factory, f)
    return {"window": {"from": _iso(start), "to": _iso(end)},
            "filters": {"team_id": team_id, "feature": feature}, **summary}


@router.get("/misses")
async def get_misses(
    request: Request,
    team_id: str | None = None,
    feature: str | None = None,
    model: str | None = Query(default=None, description="The cheap model that missed"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    start, end = _window(from_, to)
    f = QualityFilter(start=start, end=end, team_id=team_id, feature=feature)
    return await list_misses(request.app.state.session_factory, f, model, limit, offset)


@router.get("/queue")
async def get_queue(request: Request):
    try:
        return await request.app.state.queue.stats()
    except RedisError as exc:
        raise GatewayError(f"Verification queue unavailable ({type(exc).__name__})",
                           status_code=503, code="queue_unavailable", retryable=True) from exc
