"""Read-only analytics API behind the dashboard.

Every endpoint takes the same window parameters (from, to, team_id, feature; default: the last
30 days), validates them, serves from a short TTL cache, and returns JSON with money as
fixed-point strings and times as UTC ISO 8601. No auth yet (known limitation).
"""
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from app import analytics
from app.analytics.common import AnalyticsFilter, WindowError
from app.errors import GatewayError
from app.money import usd_str

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])


def jsonable(value):
    """Decimal -> fixed-point string, date/datetime -> ISO (UTC 'Z'), recursively."""
    if isinstance(value, Decimal):
        return usd_str(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def window(request: Request,
           from_: datetime | None = Query(default=None, alias="from",
                                          description="Inclusive, ISO 8601 (default: 30 days ago). "
                                                      "Naive = UTC"),
           to: datetime | None = Query(default=None, description="Exclusive (default: now)"),
           team_id: str | None = None, feature: str | None = None) -> AnalyticsFilter:
    settings = request.app.state.settings
    try:
        return analytics.make_filter(from_, to, team_id=team_id, feature=feature,
                                     max_days=settings.analytics_max_window_days)
    except WindowError as exc:
        raise GatewayError(f"Invalid window: {exc}", status_code=400,
                           code="invalid_window") from exc


async def respond(request: Request, name: str, f: AnalyticsFilter | None, compute,
                  **params) -> dict:
    """Serve from the TTL cache keyed by endpoint + parameters; stamp 'as of' times."""
    cache: analytics.TTLCache = request.app.state.analytics_cache
    key = (name, f, tuple(sorted(params.items())))

    async def build():
        data = await compute()
        return {"generated_at": datetime.now(UTC), **({"window": {
            "from": f.start, "to": f.end, "team_id": f.team_id, "feature": f.feature}}
            if f else {}), **({"params": params} if params else {}), **data}

    body, cached = await cache.get_or_compute(key, build)
    return jsonable({**body, "cached": cached})


@router.get("/summary")
async def get_summary(request: Request, f: AnalyticsFilter = Depends(window)):
    sf = request.app.state.session_factory
    return await respond(request, "summary", f, lambda: analytics.summary(sf, f))


@router.get("/spend")
async def get_spend(request: Request, f: AnalyticsFilter = Depends(window),
                    group_by: Literal["team", "feature", "model"] = "team"):
    sf = request.app.state.session_factory

    async def compute():
        return {**await analytics.spend_timeseries(sf, f, group_by),
                "by_model": await analytics.cost_by_model(sf, f)}
    return await respond(request, "spend", f, compute, group_by=group_by)


@router.get("/projections")
async def get_projections(request: Request, team_id: str | None = None,
                          feature: str | None = None, burndown: bool = True):
    """Month-end projections for the CURRENT month (no window: it's always month-to-date)."""
    sf = request.app.state.session_factory
    return await respond(
        request, "projections", None,
        lambda: analytics.projections(sf, datetime.now(UTC), team_id=team_id, feature=feature,
                                      include_burndown=burndown),
        team_id=team_id, feature=feature, burndown=burndown)


@router.get("/top")
async def get_top(request: Request, f: AnalyticsFilter = Depends(window),
                  kind: Literal["requests", "patterns"] = "patterns",
                  limit: int = Query(default=10, ge=1, le=100)):
    sf = request.app.state.session_factory
    fn = analytics.top_requests if kind == "requests" else analytics.top_patterns

    async def compute():
        return {"kind": kind, "items": await fn(sf, f, limit)}
    return await respond(request, "top", f, compute, kind=kind, limit=limit)


@router.get("/savings")
async def get_savings(request: Request, f: AnalyticsFilter = Depends(window)):
    sf = request.app.state.session_factory
    return await respond(request, "savings", f, lambda: analytics.savings(sf, f))


@router.get("/quality")
async def get_quality(request: Request, f: AnalyticsFilter = Depends(window)):
    sf = request.app.state.session_factory
    return await respond(request, "quality", f, lambda: analytics.routing_quality(sf, f))


@router.get("/latency")
async def get_latency(request: Request, f: AnalyticsFilter = Depends(window)):
    sf = request.app.state.session_factory

    async def compute():
        return {"by_model": await analytics.latency_by_model(sf, f)}
    return await respond(request, "latency", f, compute)


@router.get("/errors")
async def get_errors(request: Request, f: AnalyticsFilter = Depends(window)):
    sf = request.app.state.session_factory
    return await respond(request, "errors", f, lambda: analytics.error_breakdown(sf, f))
