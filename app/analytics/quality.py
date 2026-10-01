"""Savings (gross vs net), routing quality and latency."""
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analytics.common import (
    AnalyticsFilter,
    dialect_of,
    money,
    percentile_cont,
    ratio,
    wilson_interval,
)
from app.db.models import RequestLog, RoutingMiss, Verification


async def savings(sf: async_sessionmaker, f: AnalyticsFilter) -> dict:
    """gross = Σ baseline − Σ actual over routed successful requests;
    net = gross − verification spend (quality assurance is an overhead of routing)."""
    routed = [*f.conditions(RequestLog), RequestLog.status == "success",
              RequestLog.route_source == "routed", RequestLog.baseline_cost_usd.is_not(None)]
    async with sf() as s:
        n, baseline, actual = (await s.execute(
            select(func.count(), func.sum(RequestLog.baseline_cost_usd),
                   func.sum(RequestLog.cost_usd)).where(*routed))).one()
        by_feature = (await s.execute(
            select(RequestLog.feature, func.count(), func.sum(RequestLog.baseline_cost_usd),
                   func.sum(RequestLog.cost_usd)).where(*routed).group_by(RequestLog.feature))).all()
        by_tier = (await s.execute(
            select(RequestLog.routed_tier, func.count(), func.sum(RequestLog.baseline_cost_usd),
                   func.sum(RequestLog.cost_usd)).where(*routed)
            .group_by(RequestLog.routed_tier))).all()
        overhead = await s.scalar(select(func.sum(Verification.verification_cost_usd))
                                  .where(*f.conditions(Verification)))
        all_spend = await s.scalar(select(func.sum(RequestLog.cost_usd))
                                   .where(*f.conditions(RequestLog)))

    baseline, actual, overhead = money(baseline), money(actual), money(overhead)
    gross = baseline - actual
    net = gross - overhead

    def row(key: str, value, count, b, a) -> dict:
        b, a = money(b), money(a)
        return {key: value, "requests": int(count), "baseline_cost_usd": b, "actual_cost_usd": a,
                "gross_savings_usd": b - a, "savings_pct": _pct(b - a, b)}

    return {
        "routed_requests": int(n), "baseline_cost_usd": baseline, "actual_cost_usd": actual,
        "gross_savings_usd": gross, "gross_savings_pct": _pct(gross, baseline),
        "verification_overhead_usd": overhead, "net_savings_usd": net,
        "net_savings_pct": _pct(net, baseline), "all_requests_cost_usd": money(all_spend),
        "by_feature": sorted((row("feature", *r) for r in by_feature),
                             key=lambda x: -x["gross_savings_usd"]),
        "by_tier": sorted((row("tier", *r) for r in by_tier), key=lambda x: x["tier"] or 0),
    }


def _pct(part: Decimal, whole: Decimal) -> float | None:
    return round(float(part / whole * 100), 2) if whole else None


async def routing_quality(sf: async_sessionmaker, f: AnalyticsFilter) -> dict:
    ok = [*f.conditions(RequestLog), RequestLog.status == "success"]
    async with sf() as s:
        tiers = (await s.execute(select(RequestLog.routed_tier, func.count()).where(*ok)
                                 .group_by(RequestLog.routed_tier))).all()
        sources = (await s.execute(select(RequestLog.route_source, func.count()).where(*ok)
                                   .group_by(RequestLog.route_source))).all()
        successes = await s.scalar(select(func.count()).where(*ok)) or 0
        escalated = await s.scalar(select(func.count()).where(*ok, RequestLog.escalated.is_(True)))
        pre = await s.scalar(select(func.count()).where(*ok, RequestLog.pre_escalated.is_(True)))
        downgraded = await s.scalar(select(func.count()).where(*ok,
                                                               RequestLog.downgraded.is_(True)))
        blocks = await s.scalar(select(func.count()).where(
            *f.conditions(RequestLog), RequestLog.status == "budget_blocked"))
        overrides = await s.scalar(select(func.count()).where(
            *f.conditions(RequestLog), RequestLog.override_reason.is_not(None)))
        verdicts = (await s.execute(select(Verification.verdict, func.count())
                                    .where(*f.conditions(Verification))
                                    .group_by(Verification.verdict))).all()
        per_model = (await s.execute(
            select(Verification.model, Verification.verdict, func.count())
            .where(*f.conditions(Verification), Verification.verdict.in_(("pass", "fail")))
            .group_by(Verification.model, Verification.verdict))).all()
        misses_model = (await s.execute(select(RoutingMiss.chosen_model, func.count())
                                        .where(*f.conditions(RoutingMiss))
                                        .group_by(RoutingMiss.chosen_model))).all()
        misses_feature = (await s.execute(select(RoutingMiss.feature, func.count())
                                          .where(*f.conditions(RoutingMiss))
                                          .group_by(RoutingMiss.feature))).all()

    counts = {v: 0 for v in ("pass", "fail", "inconclusive", "skipped")}
    counts.update({v: int(n) for v, n in verdicts})
    judged = counts["pass"] + counts["fail"]
    models: dict[str, dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0})
    for model, verdict, n in per_model:
        models[model][verdict] = int(n)
    return {
        "successful_requests": int(successes),
        "tier_distribution": {str(t if t is not None else "unknown"): int(n) for t, n in tiers},
        "route_sources": {str(src or "unknown"): int(n) for src, n in sources},
        "escalation": {"post_call": int(escalated or 0), "pre_call": int(pre or 0),
                       "post_call_rate": ratio(escalated or 0, successes),
                       "pre_call_rate": ratio(pre or 0, successes)},
        "budget": {"downgrades": int(downgraded or 0), "blocks": int(blocks or 0),
                   "overrides": int(overrides or 0)},
        "verification": {**counts, "verified": sum(counts.values()), "judged": judged,
                         "pass_rate": ratio(counts["pass"], judged),
                         "pass_rate_ci95": wilson_interval(counts["pass"], judged)},
        "verification_by_model": [
            {"model": m, "judged": c["pass"] + c["fail"], "pass": c["pass"], "fail": c["fail"],
             "pass_rate": ratio(c["pass"], c["pass"] + c["fail"]),
             "pass_rate_ci95": wilson_interval(c["pass"], c["pass"] + c["fail"])}
            for m, c in sorted(models.items())],
        "misses_by_model": {m: int(n) for m, n in sorted(misses_model)},
        "misses_by_feature": {f_: int(n) for f_, n in sorted(misses_feature)},
    }


async def latency_by_model(sf: async_sessionmaker, f: AnalyticsFilter) -> list[dict]:
    """p50/p95/p99 per model. Averages hide the tail: 99 fast requests and one 30-second
    timeout average out to 'fine', while p99 shows what the unluckiest users wait."""
    conds = [*f.conditions(RequestLog), RequestLog.status == "success",
             RequestLog.latency_ms.is_not(None), RequestLog.model.is_not(None)]
    async with sf() as s:
        if dialect_of(s) == "postgresql":
            lat = RequestLog.latency_ms
            rows = (await s.execute(
                select(RequestLog.model, func.count(), func.avg(lat),
                       func.percentile_cont(0.5).within_group(lat),
                       func.percentile_cont(0.95).within_group(lat),
                       func.percentile_cont(0.99).within_group(lat))
                .where(*conds).group_by(RequestLog.model))).all()
            items = [{"model": m, "requests": int(n), "avg_ms": round(float(a), 2),
                      "p50_ms": round(float(p50), 2), "p95_ms": round(float(p95), 2),
                      "p99_ms": round(float(p99), 2)} for m, n, a, p50, p95, p99 in rows]
        else:
            # Fallback for databases without percentile_cont (SQLite in unit tests)
            rows = (await s.execute(select(RequestLog.model, RequestLog.latency_ms)
                                    .where(*conds))).all()
            values: dict[str, list[float]] = defaultdict(list)
            for model, latency in rows:
                values[model].append(float(latency))
            items = [{"model": m, "requests": len(v), "avg_ms": round(sum(v) / len(v), 2),
                      "p50_ms": percentile_cont(v, 0.5), "p95_ms": percentile_cont(v, 0.95),
                      "p99_ms": percentile_cont(v, 0.99)} for m, v in values.items()]
    return sorted(items, key=lambda x: x["model"])
