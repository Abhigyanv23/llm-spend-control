"""Rebuild Redis counters from Postgres (the source of truth).

Why this exists: Redis is a fast cache that can drift from reality.
  - Redis restarted without persistence -> counters are gone -> budgets look empty
  - The gateway crashed between reserve and settle -> a hold is stuck in :reserved
  - A settle failed while Redis was briefly unreachable -> spend is missing
Reconciliation recomputes today's and this month's spend with SUM(cost_usd) and
overwrites the counters, and resets all holds to 0.

Verification spend (Phase 4) is real spend too, recorded in `verifications`, not
`request_logs`. It is charged to the verifier's own team/feature (quality.yaml), so it is
added to those counters here; otherwise every API restart would reset the verifier budget.

Caveat: resetting holds is only safe when no requests are in flight, e.g. at startup of
a single instance. With several gateway instances this needs a lock or a quiet window.
"""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.budgets.periods import as_utc, current_periods
from app.budgets.store import Counter, RedisBudgetStore
from app.db.models import RequestLog, Verification
from app.money import usd_to_nanos


async def reconcile_counters(session_factory: async_sessionmaker, store: RedisBudgetStore,
                             now: datetime,
                             verifier_scope: tuple[str, str] | None = None) -> dict:
    """verifier_scope = (team_id, feature) that verification spend is charged to."""
    now = as_utc(now)
    periods = current_periods(now)
    spent: dict[Counter, int] = {}

    async with session_factory() as session:
        for period in periods:
            for scope, column in (("team", RequestLog.team_id), ("feature", RequestLog.feature)):
                # Served by the (team_id, created_at) / (feature, created_at) indexes
                stmt = (select(column, func.sum(RequestLog.cost_usd))
                        .where(RequestLog.created_at >= period.start,
                               RequestLog.created_at < period.resets_at)
                        .group_by(column))
                for scope_id, total in (await session.execute(stmt)).all():
                    nanos = usd_to_nanos(total or 0)
                    if nanos:
                        spent[Counter(scope, scope_id, period)] = nanos

            if verifier_scope is not None:
                total = await session.scalar(
                    select(func.coalesce(func.sum(Verification.verification_cost_usd), 0))
                    .where(Verification.created_at >= period.start,
                           Verification.created_at < period.resets_at))
                nanos = usd_to_nanos(total or 0)
                if nanos:
                    for scope, scope_id in zip(("team", "feature"), verifier_scope):
                        key = Counter(scope, scope_id, period)
                        spent[key] = spent.get(key, 0) + nanos

    rebuilt = await store.rebuild(periods, spent, now)
    return {"periods": [p.key for p in periods], "counters_rebuilt": rebuilt,
            "reconciled_at": now.isoformat().replace("+00:00", "Z")}
