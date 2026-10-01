"""Month-end spend projections and budget burn-down, per team and per feature.

Three forecasts of month-end spend, from simple to less simple:
  run-rate   MTD / elapsed_days × days_in_month
             Simple, but extrapolates whatever happened so far: a spike on day 2, or a
             weekend-heavy start, is multiplied by ~15.
  trailing   MTD + (average daily spend over the last 7 complete days) × remaining days
             Reacts to recent changes and covers a full week (weekday seasonality averages
             out), but ignores older history.
  ewma       MTD + (exponentially weighted daily average, alpha 0.3) × remaining days
             Recent days count most, older days still contribute; a middle ground.
None of these models weekly seasonality or growth explicitly (Holt-Winters / Prophet would).
"""
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analytics.common import ZERO, day_bucket, dialect_of, money, to_date
from app.budgets.periods import as_utc, month_period
from app.db.models import BudgetPolicy, RequestLog

EWMA_ALPHA = Decimal("0.3")
TRAILING_DAYS = 7
MIN_ELAPSED_DAYS = Decimal(1)       # don't project a whole month from a few hours of data
SCOPE_COLUMNS = {"team": RequestLog.team_id, "feature": RequestLog.feature}


def ewma(values: list[Decimal], alpha: Decimal = EWMA_ALPHA) -> Decimal:
    """Exponentially weighted moving average, oldest value first."""
    if not values:
        return ZERO
    smoothed = values[0]
    for value in values[1:]:
        smoothed = alpha * value + (1 - alpha) * smoothed
    return smoothed


def project(mtd: Decimal, elapsed_days: Decimal, days_in_month: int,
            trailing_daily: Decimal, ewma_daily: Decimal) -> dict:
    elapsed = max(elapsed_days, MIN_ELAPSED_DAYS)
    remaining = max(Decimal(days_in_month) - elapsed_days, ZERO)
    return {"run_rate": money(mtd / elapsed * days_in_month),
            "trailing_7d": money(mtd + trailing_daily * remaining),
            "ewma": money(mtd + ewma_daily * remaining)}


def exhaustion(mtd: Decimal, limit: Decimal | None, daily_rate: Decimal, now: datetime,
               month_end: datetime) -> tuple[str, datetime | None]:
    """(status, projected exhaustion time) for a monthly limit at the current run-rate."""
    if limit is None:
        return "no_limit", None
    if mtd >= limit:
        return "exhausted", now
    if daily_rate <= 0:
        return "ok", None
    days_left_in_month = Decimal(str((month_end - now).total_seconds() / 86400))
    days_to_exhaustion = (limit - mtd) / daily_rate
    # Compare in days BEFORE building a date: a tiny run-rate against a large limit means
    # "exhausted in 10,000 years", which overflows datetime
    if days_to_exhaustion >= days_left_in_month:
        projected_pct = (mtd + daily_rate * days_left_in_month) / limit
        return ("warning" if projected_pct >= Decimal("0.8") else "ok"), None
    return "at_risk", now + timedelta(days=float(days_to_exhaustion))


async def projections(sf: async_sessionmaker, now: datetime, *, team_id: str | None = None,
                      feature: str | None = None, include_burndown: bool = True) -> dict:
    now = as_utc(now)
    month = month_period(now)
    days_in_month = (month.resets_at - month.start).days
    elapsed_days = Decimal(str(round((now - month.start).total_seconds() / 86400, 6)))
    today = now.date()
    month_days = [month.start.date() + timedelta(days=i) for i in range(days_in_month)]
    trailing_start = datetime.combine(today - timedelta(days=TRAILING_DAYS), datetime.min.time(),
                                      tzinfo=UTC)
    window_start = min(month.start, trailing_start)
    wanted = {"team": team_id, "feature": feature}

    items = []
    async with sf() as s:
        day = day_bucket(RequestLog.created_at, dialect_of(s))
        policies = (await s.scalars(select(BudgetPolicy).where(BudgetPolicy.enabled.is_(True)))).all()
        limits = {(p.scope, p.scope_id): p.monthly_limit_usd for p in policies}
        filtered = {scope for scope, value in wanted.items() if value is not None}
        for scope, column in SCOPE_COLUMNS.items():
            if filtered and scope not in filtered:
                continue        # filtering by team only shows that team (and vice versa)
            conds = [RequestLog.created_at >= window_start, RequestLog.created_at < now]
            if wanted[scope] is not None:
                conds.append(column == wanted[scope])
            rows = (await s.execute(select(column, day, func.sum(RequestLog.cost_usd))
                                    .where(*conds).group_by(column, day))).all()
            daily: dict[str, dict[date, Decimal]] = defaultdict(dict)
            for scope_id, bucket, cost in rows:
                daily[scope_id][to_date(bucket)] = money(cost)
            ids = set(daily) | {sid for (sc, sid), lim in limits.items()
                                if sc == scope and lim is not None}
            if wanted[scope] is not None:
                ids &= {wanted[scope]}
            for scope_id in sorted(ids):
                items.append(_scope_projection(
                    scope, scope_id, daily.get(scope_id, {}), limits.get((scope, scope_id)),
                    now, month, month_days, today, days_in_month, elapsed_days, include_burndown))

    status_order = {"exhausted": 0, "at_risk": 1, "warning": 2, "ok": 3, "no_limit": 4}
    items.sort(key=lambda x: (status_order[x["status"]], -x["month_to_date_usd"]))
    return {"month": month.key, "now": now, "days_in_month": days_in_month,
            "elapsed_days": float(elapsed_days), "items": items}


def _scope_projection(scope: str, scope_id: str, by_day: dict[date, Decimal],
                      limit: Decimal | None, now: datetime, month, month_days: list[date],
                      today: date, days_in_month: int, elapsed_days: Decimal,
                      include_burndown: bool) -> dict:
    mtd = sum((v for d, v in by_day.items() if d >= month.start.date()), ZERO)
    complete = [today - timedelta(days=i) for i in range(TRAILING_DAYS, 0, -1)]
    trailing_daily = sum((by_day.get(d, ZERO) for d in complete), ZERO) / TRAILING_DAYS
    month_complete = [d for d in month_days if d < today]
    ewma_daily = ewma([by_day.get(d, ZERO) for d in month_complete]) if month_complete else (
        mtd / max(elapsed_days, MIN_ELAPSED_DAYS))
    forecasts = project(mtd, elapsed_days, days_in_month, trailing_daily, ewma_daily)
    daily_rate = mtd / max(elapsed_days, MIN_ELAPSED_DAYS)
    status, exhausted_at = exhaustion(mtd, limit, daily_rate, now, month.resets_at)
    item = {"scope": scope, "scope_id": scope_id, "monthly_limit_usd": limit,
            "month_to_date_usd": money(mtd), "daily_run_rate_usd": money(daily_rate),
            "projected_usd": forecasts,
            "projected_pct_of_limit": (round(float(forecasts["run_rate"] / limit * 100), 1)
                                       if limit else None),
            "status": status, "projected_exhaustion_at": exhausted_at}
    if include_burndown and limit is not None:
        item["burndown"] = _burndown(by_day, limit, month_days, today, daily_rate, mtd)
    return item


def _burndown(by_day: dict[date, Decimal], limit: Decimal, month_days: list[date], today: date,
              daily_rate: Decimal, mtd: Decimal) -> list[dict]:
    """Cumulative spend so far, the 'ideal' straight line to the limit, and the run-rate
    projection for the rest of the month."""
    points, cumulative = [], ZERO
    n = len(month_days)
    for i, d in enumerate(month_days, start=1):
        if d <= today:
            cumulative += by_day.get(d, ZERO)
        future = d > today
        points.append({"date": d,
                       "cumulative_usd": None if future else money(cumulative),
                       "ideal_usd": money(limit * i / n),
                       "projected_usd": money(mtd + daily_rate * (d - today).days) if future else None,
                       "limit_usd": limit})
    return points
