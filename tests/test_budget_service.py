"""BudgetService: warn / block / override / fail-open / fail-closed / reconcile."""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.budgets import BudgetService
from app.budgets.periods import current_periods
from app.budgets.store import Counter
from app.db.models import BudgetAlert, RequestLog
from app.errors import BudgetExceededError, BudgetUnavailableError, OverrideRequiredError
from app.schemas import Priority

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


async def reserve(budgets: BudgetService, estimate: str, priority=Priority.normal,
                  override=None, team="search", feature="summarize", now=NOW):
    return await budgets.reserve(request_id=str(uuid.uuid4()), team_id=team, feature=feature,
                                 priority=priority, estimate_usd=Decimal(estimate),
                                 override_reason=override, now=now)


async def spent_reserved(redis_client, scope, scope_id, period_name="day", now=NOW):
    period = next(p for p in current_periods(now) if p.name == period_name)
    c = Counter(scope, scope_id, period)
    spent, reserved = await redis_client.mget(c.spent_key, c.reserved_key)
    return int(spent or 0), int(reserved or 0)


async def test_no_policy_allows_and_still_tracks_spend(budgets, redis_client):
    r = await reserve(budgets, "0.50")
    assert r.status == "ok" and r.warning_header() is None
    await budgets.settle(r, Decimal("0.10"))
    assert await spent_reserved(redis_client, "team", "search") == (100_000_000, 0)
    assert await spent_reserved(redis_client, "feature", "summarize", "month") == (100_000_000, 0)


async def test_warning_at_80_percent_and_alert_recorded_once(budgets, set_policy,
                                                             session_factory):
    await set_policy("team", "search", daily="1.00")
    for _ in range(2):
        r = await reserve(budgets, "0.85")
        assert r.status == "warning"
        assert r.warning_header() == "team:search:day=85.0%"
        assert r.metadata()["budget_warnings"][0]["percent_used"] == 85.0
        await budgets.release(r)
    async with session_factory() as s:
        alerts = (await s.scalars(select(BudgetAlert))).all()
    assert len(alerts) == 1
    assert (alerts[0].threshold, alerts[0].period_key) == (Decimal("0.80"), "2026-09-30")


async def test_low_priority_blocked_at_100_percent(budgets, set_policy, redis_client):
    await set_policy("team", "search", daily="1.00")
    with pytest.raises(BudgetExceededError) as exc:
        await reserve(budgets, "1.00", Priority.low)
    err = exc.value
    assert (err.status_code, err.code) == (402, "budget_exceeded")
    assert err.extra["scope"] == "team" and err.extra["period"] == "day"
    assert err.extra["resets_at"] == "2026-10-01T00:00:00Z"
    assert "Team 'search' daily budget of $1.00 reached" in err.message
    assert await spent_reserved(redis_client, "team", "search") == (0, 0)   # nothing held


async def test_low_priority_cannot_use_override(budgets, set_policy):
    await set_policy("team", "search", daily="1.00")
    with pytest.raises(BudgetExceededError) as exc:
        await reserve(budgets, "2.00", Priority.normal, override="please")
    assert "only honoured for high/critical" in exc.value.message


async def test_high_priority_needs_override(budgets, set_policy):
    await set_policy("team", "search", daily="1.00")
    with pytest.raises(OverrideRequiredError) as exc:
        await reserve(budgets, "2.00", Priority.high)
    assert (exc.value.status_code, exc.value.code) == (402, "override_required")


async def test_high_priority_with_override_is_allowed_and_flagged(budgets, set_policy,
                                                                  redis_client):
    await set_policy("team", "search", daily="1.00")
    r = await reserve(budgets, "2.00", Priority.critical, override="incident INC-42")
    assert r.status == "overridden" and r.override_reason == "incident INC-42"
    assert "(overridden)" in r.warning_header()
    assert await spent_reserved(redis_client, "team", "search") == (0, 2_000_000_000)


async def test_strictest_scope_wins(budgets, set_policy):
    await set_policy("team", "search", daily="100")
    await set_policy("feature", "summarize", daily="0.50")
    with pytest.raises(BudgetExceededError) as exc:
        await reserve(budgets, "1.00")
    assert (exc.value.extra["scope"], exc.value.extra["scope_id"]) == ("feature", "summarize")


async def test_monthly_limit_enforced(budgets, set_policy):
    await set_policy("team", "search", daily=None, monthly="3.00")
    for _ in range(2):
        await budgets.settle(await reserve(budgets, "1.00"), Decimal("1.00"))
    with pytest.raises(BudgetExceededError) as exc:
        await reserve(budgets, "1.00")                   # 2 + 1 >= 3
    assert exc.value.extra["period"] == "month"


async def test_zero_limit_blocks_everything(budgets, set_policy):
    await set_policy("team", "search", daily="0")
    with pytest.raises(BudgetExceededError):
        await reserve(budgets, "0")


async def test_disabled_policy_is_ignored(budgets, set_policy):
    await set_policy("team", "search", daily="0", enabled=False)
    assert (await reserve(budgets, "1.00")).status == "ok"


async def test_settle_uses_the_reservation_period_across_midnight(budgets, redis_client):
    late = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
    r = await reserve(budgets, "0.10", now=late)
    await budgets.settle(r, Decimal("0.05"))              # "finishes" after midnight
    assert await spent_reserved(redis_client, "team", "search", now=late) == (50_000_000, 0)


async def test_fail_open_allows_when_redis_down(budgets, redis_server):
    redis_server.connected = False
    r = await reserve(budgets, "1.00")
    assert r.held is False and r.status == "unchecked"
    assert r.warning_header() == "budget-check-unavailable"
    await budgets.settle(r, Decimal("0.5"))                # must not raise


async def test_fail_closed_rejects_when_redis_down(store, session_factory, redis_server):
    budgets = BudgetService(store, session_factory, fail_mode="closed")
    redis_server.connected = False
    with pytest.raises(BudgetUnavailableError) as exc:
        await reserve(budgets, "1.00")
    assert (exc.value.status_code, exc.value.code) == (503, "budget_unavailable")


async def test_reconcile_rebuilds_counters_from_request_logs(budgets, session_factory,
                                                             redis_client):
    stuck = await reserve(budgets, "9.00")                 # a hold that will never settle
    assert stuck.held
    async with session_factory() as s:
        for created, cost in [(NOW, "0.25"), (NOW - timedelta(hours=1), "0.50"),
                              (NOW - timedelta(days=5), "1.00"),      # this month only
                              (NOW - timedelta(days=40), "7.00")]:    # last month: ignored
            s.add(RequestLog(id=uuid.uuid4(), created_at=created, team_id="search",
                             feature="summarize", priority="normal", status="success",
                             cost_usd=Decimal(cost), meta={}))
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(RequestLog)) == 4

    summary = await budgets.reconcile(NOW)
    assert summary["periods"] == ["2026-09-30", "2026-09"]
    assert await spent_reserved(redis_client, "team", "search") == (750_000_000, 0)
    assert await spent_reserved(redis_client, "team", "search", "month") == (1_750_000_000, 0)
