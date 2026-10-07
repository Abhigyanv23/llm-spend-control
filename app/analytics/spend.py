"""Spend: time series, cost by model/provider, top requests and prompt patterns, errors."""
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analytics.common import (ZERO, AnalyticsFilter, day_bucket, dialect_of, money, ratio,
                                  to_date)
from app.db.models import RequestLog

GROUP_COLUMNS = {"team": RequestLog.team_id, "feature": RequestLog.feature,
                 "model": RequestLog.model}
ERROR_STATUSES = ("provider_error", "internal_error")


async def spend_timeseries(sf: async_sessionmaker, f: AnalyticsFilter,
                           group_by: str = "team") -> dict:
    """Daily cost per group, zero-filled so every series has a point for every UTC day
    (charts would otherwise draw misleading straight lines across days without traffic)."""
    column = func.coalesce(GROUP_COLUMNS[group_by], "unknown")
    async with sf() as s:
        day = day_bucket(RequestLog.created_at, dialect_of(s))
        rows = (await s.execute(
            select(day, column, func.sum(RequestLog.cost_usd), func.count())
            .where(*f.conditions(RequestLog)).group_by(day, column))).all()

    days = f.days()
    cells: dict[str, dict] = defaultdict(dict)
    for bucket, key, cost, count in rows:
        cells[key][to_date(bucket)] = (money(cost), int(count))
    series = []
    for key, by_day in cells.items():
        points = [{"date": d, "cost_usd": by_day.get(d, (ZERO, 0))[0],
                   "requests": by_day.get(d, (ZERO, 0))[1]} for d in days]
        series.append({"key": key, "total_cost_usd": sum((p["cost_usd"] for p in points), ZERO),
                       "requests": sum(p["requests"] for p in points), "points": points})
    series.sort(key=lambda x: (-x["total_cost_usd"], x["key"]))
    total = sum((x["total_cost_usd"] for x in series), ZERO)
    return {"group_by": group_by, "days": days, "total_cost_usd": total, "series": series}


async def cost_by_model(sf: async_sessionmaker, f: AnalyticsFilter) -> list[dict]:
    async with sf() as s:
        rows = (await s.execute(
            select(RequestLog.model, RequestLog.provider, func.count(),
                   func.sum(RequestLog.cost_usd), func.sum(RequestLog.input_tokens),
                   func.sum(RequestLog.output_tokens))
            .where(*f.conditions(RequestLog), RequestLog.model.is_not(None))
            .group_by(RequestLog.model, RequestLog.provider))).all()
    items = [{"model": m, "provider": p or "unknown", "requests": int(n), "cost_usd": money(c),
              "input_tokens": int(i or 0), "output_tokens": int(o or 0),
              "avg_cost_usd": money(Decimal(money(c)) / n) if n else ZERO}
             for m, p, n, c, i, o in rows]
    return sorted(items, key=lambda x: (-x["cost_usd"], x["model"]))


async def top_requests(sf: async_sessionmaker, f: AnalyticsFilter, limit: int = 10) -> list[dict]:
    async with sf() as s:
        rows = (await s.scalars(
            select(RequestLog).where(*f.conditions(RequestLog))
            .order_by(RequestLog.cost_usd.desc(), RequestLog.created_at.desc())
            .limit(limit))).all()
    return [{"request_id": str(r.id), "created_at": r.created_at, "team_id": r.team_id,
             "feature": r.feature, "model": r.model, "routed_tier": r.routed_tier,
             "cost_usd": money(r.cost_usd), "input_tokens": r.input_tokens,
             "output_tokens": r.output_tokens, "escalated": r.escalated,
             "prompt_fingerprint": r.prompt_fingerprint, "prompt_preview": r.prompt_preview}
            for r in rows]


async def top_patterns(sf: async_sessionmaker, f: AnalyticsFilter, limit: int = 10) -> list[dict]:
    """The most expensive prompt *patterns* (same fingerprint), not single requests: this is
    where a prompt rewrite, caching or a cheaper route saves the most."""
    conds = [*f.conditions(RequestLog), RequestLog.prompt_fingerprint.is_not(None)]
    async with sf() as s:
        rows = (await s.execute(
            select(RequestLog.prompt_fingerprint, func.count(), func.sum(RequestLog.cost_usd),
                   func.max(RequestLog.prompt_preview))
            .where(*conds).group_by(RequestLog.prompt_fingerprint)
            .order_by(func.sum(RequestLog.cost_usd).desc(), RequestLog.prompt_fingerprint)
            .limit(limit))).all()
        fingerprints = [r[0] for r in rows]
        model_rows = (await s.execute(
            select(RequestLog.prompt_fingerprint, RequestLog.model, func.count())
            .where(*conds, RequestLog.prompt_fingerprint.in_(fingerprints))
            .group_by(RequestLog.prompt_fingerprint, RequestLog.model))).all() if rows else []
    models: dict[str, dict[str, int]] = defaultdict(dict)
    for fp, model, count in model_rows:
        models[fp][model or "unknown"] = int(count)
    return [{"prompt_fingerprint": fp, "requests": int(n), "total_cost_usd": money(c),
             "avg_cost_usd": money(Decimal(money(c)) / n) if n else ZERO,
             "models": dict(sorted(models[fp].items())), "prompt_preview": preview}
            for fp, n, c, preview in rows]


async def error_breakdown(sf: async_sessionmaker, f: AnalyticsFilter) -> dict:
    """Error rate by provider (provider/internal errors over all requests that reached
    routing) and counts by error code and status."""
    # Build the grouping expression ONCE and reuse it: two separate coalesce(...) objects
    # become two different bind parameters, and Postgres then rejects the GROUP BY
    provider = func.coalesce(RequestLog.provider, "none")
    async with sf() as s:
        by_provider = (await s.execute(
            select(provider, func.count(),
                   func.sum(case((RequestLog.status.in_(ERROR_STATUSES), 1), else_=0)))
            .where(*f.conditions(RequestLog)).group_by(provider))).all()
        by_code = (await s.execute(
            select(RequestLog.error_code, RequestLog.status, func.count())
            .where(*f.conditions(RequestLog), RequestLog.status != "success")
            .group_by(RequestLog.error_code, RequestLog.status))).all()
        by_status = (await s.execute(
            select(RequestLog.status, func.count()).where(*f.conditions(RequestLog))
            .group_by(RequestLog.status))).all()
    providers = [{"provider": p, "requests": int(n), "errors": int(e or 0),
                  "error_rate": ratio(e or 0, n)} for p, n, e in by_provider]
    codes = [{"error_code": c or "none", "status": st, "count": int(n)} for c, st, n in by_code]
    return {"by_provider": sorted(providers, key=lambda x: -x["requests"]),
            "by_error_code": sorted(codes, key=lambda x: (-x["count"], x["error_code"])),
            "by_status": {st: int(n) for st, n in by_status}}
