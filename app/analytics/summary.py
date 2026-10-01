"""Headline KPIs for the dashboard's overview page."""
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analytics.common import AnalyticsFilter, money, ratio
from app.analytics.projections import projections
from app.analytics.quality import routing_quality, savings
from app.budgets.periods import as_utc, current_periods
from app.db.models import BudgetAlert, RequestLog


async def summary(sf: async_sessionmaker, f: AnalyticsFilter, now: datetime | None = None) -> dict:
    now = as_utc(now or datetime.now(UTC))
    day, month = current_periods(now)

    def scoped(start, end) -> list:
        return AnalyticsFilter(start, end, f.team_id, f.feature).conditions(RequestLog)

    async with sf() as s:
        window_n, window_cost, window_errors = (await s.execute(
            select(func.count(), func.sum(RequestLog.cost_usd),
                   func.count().filter(RequestLog.status.in_(("provider_error", "internal_error"))))
            .where(*f.conditions(RequestLog)))).one()
        today = await s.scalar(select(func.sum(RequestLog.cost_usd)).where(*scoped(day.start, now)))
        mtd = await s.scalar(select(func.sum(RequestLog.cost_usd))
                             .where(*scoped(month.start, now)))
        alert_conds = [or_(BudgetAlert.period_key == day.key, BudgetAlert.period_key == month.key)]
        if f.team_id is not None or f.feature is not None:
            alert_conds.append(or_(
                (BudgetAlert.scope == "team") & (BudgetAlert.scope_id == f.team_id),
                (BudgetAlert.scope == "feature") & (BudgetAlert.scope_id == f.feature)))
        alerts = (await s.scalars(select(BudgetAlert).where(*alert_conds)
                                  .order_by(BudgetAlert.created_at.desc()).limit(20))).all()

    proj = await projections(sf, now, team_id=f.team_id, feature=f.feature,
                             include_burndown=False)
    scope = "feature" if f.feature is not None and f.team_id is None else "team"
    month_end = sum((i["projected_usd"]["run_rate"] for i in proj["items"]
                     if i["scope"] == scope), money(0))
    at_risk = [{"scope": i["scope"], "scope_id": i["scope_id"], "status": i["status"],
                "projected_pct_of_limit": i["projected_pct_of_limit"]}
               for i in proj["items"] if i["status"] in ("exhausted", "at_risk")]
    sav = await savings(sf, f)
    quality = await routing_quality(sf, f)

    return {
        "requests": int(window_n), "cost_usd": money(window_cost),
        "error_rate": ratio(window_errors or 0, window_n),
        "spend_today_usd": money(today), "spend_month_to_date_usd": money(mtd),
        "projected_month_end_usd": month_end,
        "gross_savings_pct": sav["gross_savings_pct"], "net_savings_pct": sav["net_savings_pct"],
        "net_savings_usd": sav["net_savings_usd"],
        "verifier_pass_rate": quality["verification"]["pass_rate"],
        "verifier_pass_rate_ci95": quality["verification"]["pass_rate_ci95"],
        "escalation_rate": quality["escalation"]["post_call_rate"],
        "scopes_at_risk": at_risk,
        "active_budget_alerts": [{"scope": a.scope, "scope_id": a.scope_id, "period": a.period,
                                  "period_key": a.period_key, "threshold": float(a.threshold),
                                  "projected_usd": a.projected_usd, "limit_usd": a.limit_usd,
                                  "created_at": a.created_at} for a in alerts],
    }
