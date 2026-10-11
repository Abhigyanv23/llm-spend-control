"""Analytics functions on SQLite with hand-built rows and exact expected results."""
import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app import analytics
from app.analytics.common import AnalyticsFilter, WindowError, make_filter
from app.analytics.projections import exhaustion, project
from app.budgets.policies import upsert_policy
from app.db.models import RequestLog, RoutingMiss, Verification

D = Decimal


def at(day: int, hour: int = 12, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


def log(created_at, team="a", feature="f", cost="0", baseline=None, tier=1, source="routed",
        model="mock-echo", provider="mock", status="success", latency=50.0, escalated=False,
        pre=False, downgraded=False, fp=None, preview=None, override=None, error_code=None):
    return RequestLog(id=uuid.uuid4(), created_at=created_at, team_id=team, feature=feature,
                      priority="normal", model=model, provider=provider, status=status,
                      cost_usd=D(cost), baseline_cost_usd=None if baseline is None else D(baseline),
                      routed_tier=tier, route_source=source, latency_ms=latency,
                      escalated=escalated, pre_escalated=pre, downgraded=downgraded,
                      prompt_fingerprint=fp, prompt_preview=preview, override_reason=override,
                      error_code=error_code, input_tokens=100, output_tokens=50, meta={})


@pytest.fixture
def add(session_factory):
    async def _add(*rows):
        async with session_factory() as s:
            s.add_all(rows)
            await s.commit()
    return _add


def window(d1: int, d2: int) -> AnalyticsFilter:
    """Whole UTC days d1..d2 of October 2026."""
    return AnalyticsFilter(start=datetime(2026, 10, d1, tzinfo=UTC),
                           end=datetime(2026, 10, d2, tzinfo=UTC) + timedelta(days=1))


# ------------------------------------------------------------------ statistics helpers

def test_wilson_interval():
    assert analytics.wilson_interval(8, 10) == (0.4902, 0.9433)
    assert analytics.wilson_interval(0, 0) is None
    low, high = analytics.wilson_interval(10, 10)
    assert high == 1.0 and low < 0.75          # 10/10 is NOT "certainly 100%"
    assert analytics.wilson_interval(0, 10)[0] == 0.0


def test_percentile_cont_matches_postgres_interpolation():
    values = [1, 2, 3, 4]
    assert analytics.percentile_cont(values, 0.5) == 2.5
    assert analytics.percentile_cont(values, 0.95) == 3.85
    assert analytics.percentile_cont(values, 0.99) == 3.97
    assert analytics.percentile_cont([], 0.5) is None
    assert analytics.percentile_cont([7], 0.99) == 7


def test_ewma():
    assert analytics.ewma([D(1), D(2), D(3)]) == D("1.810")
    assert analytics.ewma([]) == 0


def test_make_filter_defaults_and_validation():
    now = datetime(2026, 10, 2, tzinfo=UTC)
    f = make_filter(None, None, now=now)
    assert (f.start, f.end) == (now - timedelta(days=30), now)
    naive = make_filter(datetime(2026, 9, 1), datetime(2026, 9, 2))
    assert naive.start.tzinfo is not None
    with pytest.raises(WindowError, match="earlier"):
        make_filter(now, now)
    with pytest.raises(WindowError, match="366"):
        make_filter(now - timedelta(days=400), now)
    assert window(1, 3).days() == [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]


# ------------------------------------------------------------------ spend

async def test_spend_timeseries_is_zero_filled(add, session_factory):
    await add(log(at(1), team="a", cost="1.00"), log(at(3), team="a", cost="3.00"),
              log(at(2), team="b", cost="10.00"),
              log(at(5), team="a", cost="99"))                       # outside the window
    result = await analytics.spend_timeseries(session_factory, window(1, 3), "team")
    assert result["total_cost_usd"] == D("14.00000000")
    b, a = result["series"]                                          # sorted by total desc
    assert (b["key"], a["key"]) == ("b", "a")
    assert [p["cost_usd"] for p in a["points"]] == [D(1), D(0), D(3)]
    assert [p["requests"] for p in b["points"]] == [0, 1, 0]
    assert [p["date"] for p in a["points"]] == window(1, 3).days()


async def test_cost_by_model(add, session_factory):
    await add(log(at(1), model="mock-echo", cost="1"), log(at(1), model="mock-echo", cost="2"),
              log(at(1), model="mock-large", provider="mock", cost="9"))
    large, echo = await analytics.cost_by_model(session_factory, window(1, 1))
    assert (large["model"], large["cost_usd"]) == ("mock-large", D(9))
    assert (echo["requests"], echo["avg_cost_usd"], echo["input_tokens"]) == (2, D("1.5"), 200)


async def test_top_requests_and_patterns(add, session_factory):
    await add(log(at(1), cost="1", fp="x", model="mock-echo", preview="Summarise ticket 1"),
              log(at(1), cost="2", fp="x", model="mock-echo"),
              log(at(1), cost="3", fp="x", model="mock-medium"),
              log(at(1), cost="10", fp="y", model="mock-large"),
              log(at(1), cost="50"))                                 # no fingerprint
    patterns = await analytics.top_patterns(session_factory, window(1, 1), limit=5)
    assert [(p["prompt_fingerprint"], p["requests"], p["total_cost_usd"]) for p in patterns] == [
        ("y", 1, D(10)), ("x", 3, D(6))]
    assert patterns[1]["avg_cost_usd"] == D(2)
    assert patterns[1]["models"] == {"mock-echo": 2, "mock-medium": 1}
    assert patterns[1]["prompt_preview"] == "Summarise ticket 1"
    top = await analytics.top_requests(session_factory, window(1, 1), limit=2)
    assert [r["cost_usd"] for r in top] == [D(50), D(10)]


async def test_error_breakdown(add, session_factory):
    await add(log(at(1)), log(at(1)),
              log(at(1), status="provider_error", error_code="provider_error"),
              log(at(1), status="budget_blocked", error_code="budget_exceeded", provider=None))
    errors = await analytics.error_breakdown(session_factory, window(1, 1))
    mock = next(p for p in errors["by_provider"] if p["provider"] == "mock")
    assert (mock["requests"], mock["errors"], mock["error_rate"]) == (3, 1, 0.3333)
    assert errors["by_status"] == {"success": 2, "provider_error": 1, "budget_blocked": 1}
    assert {c["error_code"] for c in errors["by_error_code"]} == {"provider_error",
                                                                  "budget_exceeded"}


# ------------------------------------------------------------------ savings and quality

async def test_gross_and_net_savings(add, session_factory):
    await add(log(at(1), feature="f1", tier=1, cost="2", baseline="10"),
              log(at(1), feature="f2", tier=2, cost="3", baseline="5"),
              log(at(1), source="explicit", cost="7", baseline="7"),      # not routing savings
              log(at(1), status="provider_error", cost="0", baseline=None))
    async with session_factory() as s:
        s.add(Verification(request_id=uuid.uuid4(), created_at=at(1), team_id="a", feature="f1",
                           model="mock-echo", tier=1, routing_source="routed", verdict="pass",
                           verification_cost_usd=D("1.5"), meta={}))
        await s.commit()
    result = await analytics.savings(session_factory, window(1, 1))
    assert (result["baseline_cost_usd"], result["actual_cost_usd"]) == (D(15), D(5))
    assert result["gross_savings_usd"] == D(10) and result["gross_savings_pct"] == 66.67
    assert result["verification_overhead_usd"] == D("1.5")
    assert result["net_savings_usd"] == D("8.5") and result["net_savings_pct"] == 56.67
    assert result["all_requests_cost_usd"] == D(12)
    assert [(t["tier"], t["gross_savings_usd"]) for t in result["by_tier"]] == [(1, D(8)), (2, D(2))]


async def test_routing_quality(add, session_factory):
    await add(log(at(1), tier=1), log(at(1), tier=1, escalated=True), log(at(1), tier=2, pre=True),
              log(at(1), tier=1, downgraded=True, override="incident"),
              log(at(1), status="budget_blocked", tier=None))
    async with session_factory() as s:
        for verdict in ["pass"] * 8 + ["fail"] * 2 + ["inconclusive"]:
            s.add(Verification(request_id=uuid.uuid4(), created_at=at(1), team_id="a",
                               feature="f", model="mock-echo", tier=1, routing_source="routed",
                               verdict=verdict, meta={}))
        s.add(RoutingMiss(request_id=uuid.uuid4(), created_at=at(1), team_id="a", feature="f",
                          chosen_model="mock-echo", chosen_tier=1, better_model="mock-large",
                          better_tier=3))
        await s.commit()
    q = await analytics.routing_quality(session_factory, window(1, 1))
    assert q["tier_distribution"] == {"1": 3, "2": 1}
    assert q["escalation"] == {"post_call": 1, "pre_call": 1, "post_call_rate": 0.25,
                               "pre_call_rate": 0.25}
    assert q["budget"] == {"downgrades": 1, "blocks": 1, "overrides": 1}
    v = q["verification"]
    assert (v["judged"], v["pass_rate"], v["pass_rate_ci95"]) == (10, 0.8, (0.4902, 0.9433))
    assert v["inconclusive"] == 1 and v["verified"] == 11
    assert q["misses_by_model"] == {"mock-echo": 1}


async def test_latency_percentiles_sqlite_fallback(add, session_factory):
    await add(*(log(at(1), model="mock-echo", latency=float(ms)) for ms in (10, 20, 30, 40)),
              log(at(1), model="mock-large", latency=500.0),
              log(at(1), model="mock-echo", latency=9999.0, status="provider_error"))
    echo, large = await analytics.latency_by_model(session_factory, window(1, 1))
    assert (echo["requests"], echo["avg_ms"], echo["p50_ms"], echo["p95_ms"]) == (4, 25.0, 25.0, 38.5)
    assert large["p99_ms"] == 500.0


# ------------------------------------------------------------------ projections

def test_projection_formulas():
    forecasts = project(mtd=D("10.5"), elapsed_days=D("10.5"), days_in_month=31,
                        trailing_daily=D(1), ewma_daily=D(2))
    assert forecasts == {"run_rate": D(31), "trailing_7d": D(31), "ewma": D("51.5")}
    # A few hours into the month: elapsed is floored at 1 day, not extrapolated x100
    early = project(mtd=D(1), elapsed_days=D("0.25"), days_in_month=30,
                    trailing_daily=D(0), ewma_daily=D(0))
    assert early["run_rate"] == D(30)


def test_exhaustion_status():
    now, end = at(11), datetime(2026, 11, 1, tzinfo=UTC)
    assert exhaustion(D(5), None, D(1), now, end) == ("no_limit", None)
    assert exhaustion(D(30), D(25), D(1), now, end) == ("exhausted", now)
    status, when = exhaustion(D("10.5"), D(25), D(1), now, end)
    assert (status, when) == ("at_risk", now + timedelta(days=14.5))
    assert exhaustion(D("10.5"), D(100), D(1), now, end) == ("ok", None)
    assert exhaustion(D("10.5"), D(35), D(1), now, end)[0] == "warning"    # 88% projected
    # Regression: a tiny run-rate against a big limit used to overflow datetime
    assert exhaustion(D("0.00000001"), D(100), D("0.00000001"), now, end) == ("ok", None)


async def test_projections_end_to_end(add, session_factory):
    now = datetime(2026, 10, 11, 12, tzinfo=UTC)               # 10.5 days into October
    await add(*(log(at(d, 6), team="t", cost="1") for d in range(1, 11)),
              log(at(11, 6), team="t", cost="0.5"),
              *(log(at(d, 6), team="u", cost="1") for d in range(1, 11)),
              log(at(11, 6), team="u", cost="0.5"),
              log(at(25, 6, month=9), team="t", cost="100"))   # last month: ignored
    async with session_factory() as s:
        await upsert_policy(s, "team", "t", None, D(25))
        await upsert_policy(s, "team", "u", None, D(100))
        await upsert_policy(s, "team", "idle", None, D(50))     # a limit but no spend yet
        await s.commit()

    result = await analytics.projections(session_factory, now)
    assert (result["month"], result["days_in_month"], result["elapsed_days"]) == ("2026-10", 31, 10.5)
    by_id = {(i["scope"], i["scope_id"]): i for i in result["items"]}
    t = by_id[("team", "t")]
    assert t["month_to_date_usd"] == D("10.5")
    assert t["projected_usd"] == {"run_rate": D(31), "trailing_7d": D(31), "ewma": D(31)}
    assert (t["projected_pct_of_limit"], t["status"]) == (124.0, "at_risk")
    assert t["projected_exhaustion_at"] == datetime(2026, 10, 26, 0, tzinfo=UTC)
    assert by_id[("team", "u")]["status"] == "ok"
    assert by_id[("team", "idle")]["month_to_date_usd"] == 0
    assert result["items"][0]["scope_id"] == "t"               # most urgent first

    burndown = t["burndown"]
    assert len(burndown) == 31 and burndown[10]["cumulative_usd"] == D("10.5")
    assert burndown[11]["cumulative_usd"] is None and burndown[11]["projected_usd"] == D("11.5")
    assert burndown[-1]["ideal_usd"] == D(25)
    feature = by_id[("feature", "f")]
    assert feature["month_to_date_usd"] == D(21) and feature["monthly_limit_usd"] is None


async def test_summary(add, session_factory):
    now = datetime(2026, 10, 11, 12, tzinfo=UTC)
    await add(log(at(11, 6), cost="2", baseline="10"), log(at(5), cost="3", baseline="5"),
              log(at(11, 7), status="provider_error"))
    s = await analytics.summary(session_factory, window(1, 11), now=now)
    assert (s["requests"], s["cost_usd"], s["error_rate"]) == (3, D(5), 0.3333)
    assert (s["spend_today_usd"], s["spend_month_to_date_usd"]) == (D(2), D(5))
    assert s["gross_savings_pct"] == 66.67 and s["active_budget_alerts"] == []


# ------------------------------------------------------------------ cache

async def test_ttl_cache_expiry_and_lru():
    cache = analytics.TTLCache(ttl_s=0.05, max_entries=2)
    calls = []

    async def compute():
        calls.append(1)
        return len(calls)
    assert await cache.get_or_compute(("k",), compute) == (1, False)
    assert await cache.get_or_compute(("k",), compute) == (1, True)
    await asyncio.sleep(0.06)
    assert await cache.get_or_compute(("k",), compute) == (2, False)       # expired
    cache.set(("a",), 1)
    cache.set(("b",), 2)
    cache.set(("c",), 3)                                                    # evicts the LRU
    assert cache.get(("k",)) == (False, None) and cache.get(("c",)) == (True, 3)
    assert analytics.TTLCache(ttl_s=0).set(("x",), 1) is None               # disabled
