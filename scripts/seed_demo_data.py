"""Seed realistic demo data for the dashboard (Phase 5).

    python scripts/seed_demo_data.py              # (re)generate ~45 days of demo traffic
    python scripts/seed_demo_data.py --days 60 --seed 7
    python scripts/seed_demo_data.py --reset      # delete the demo data and exit

Writes request_logs (+ verifications, routing_misses, budget_alerts, budget_policies) for a
fixed set of demo-* teams, with weekday patterns, a traffic spike, one team trending over its
monthly budget, a tier mix, escalations, downgrades, blocks and errors. Deterministic (seeded),
idempotent (previous demo rows are replaced), and only ever touches the DEMO_TEAMS below.

Redis budget counters are NOT touched. Real-time enforcement for demo teams only learns about
this spend after a reconciliation: restart the API or POST /v1/budgets/reconcile.
"""
import argparse
import asyncio
import math
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # make `app` importable

from sqlalchemy import delete  # noqa: E402

from app.budgets.periods import month_period  # noqa: E402
from app.budgets.policies import upsert_policy  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import create_engine, create_session_factory  # noqa: E402
from app.db.models import (  # noqa: E402
    BudgetAlert,
    BudgetPolicy,
    RequestLog,
    RoutingMiss,
    Verification,
)
from app.fingerprint import fingerprint_text  # noqa: E402
from app.money import quantize_usd  # noqa: E402
from app.registry import ModelRegistry  # noqa: E402

# team -> (base requests per weekday, monthly limit USD or None, features)
# Feature names avoid those with seeded budgets (summarize, chat-assistant): a FEATURE budget
# applies across all teams, so demo traffic would otherwise consume a real budget.
DEMO_TEAMS = {
    "demo-search":    (90, Decimal("60.00"), ["summaries", "extraction"]),
    "demo-support":   (70, Decimal("45.00"), ["assistant", "summaries"]),
    "demo-research":  (30, Decimal("40.00"), ["analysis", "code-review"]),
    "demo-marketing": (14, None, ["copywriting", "translation"]),   # limit set from its trend
}
TIER_MODELS = {1: "mock-echo", 2: "mock-medium", 3: "mock-large"}
BASELINE_MODEL = "mock-large"

# (template, true tier, typical input tokens, typical output tokens)
TEMPLATES = {
    "extraction": [("Extract the invoice number and total from document {n}", 1, 400, 60),
                   ("Convert this address list {n} to CSV", 1, 600, 200),
                   ("Fix typos in paragraph {n}", 1, 250, 220)],
    "summaries": [("Summarize support ticket #{n} in three bullet points", 2, 900, 150),
                  ("Summarise the meeting notes from week {n}", 2, 1800, 300),
                  ("Give me the key points of article {n}", 2, 1500, 250)],
    "assistant": [("How do I reset my password? (session {n})", 1, 300, 180),
                       ("Explain why my order {n} was delayed", 2, 500, 300),
                       ("Hi, what are your opening hours? {n}", 1, 120, 60)],
    "analysis": [("Analyze the trade-offs between design A and B for project {n}", 3, 2200, 900),
                 ("Compare these two strategies and recommend one ({n})", 3, 1800, 800),
                 ("Plan a migration for service {n} step by step", 3, 1500, 1000)],
    "code-review": [("Review this pull request {n} for bugs", 2, 3000, 500),
                    ("Debug the failing test in module {n}", 3, 2500, 700)],
    "copywriting": [("Rewrite this product description {n} in a friendly tone", 2, 500, 300),
                    ("Write three taglines for campaign {n}", 2, 200, 150)],
    "translation": [("Translate this paragraph {n} into German", 2, 400, 450),
                    ("Translate the FAQ entry {n} into Spanish", 2, 600, 650)],
}


def lognormal(rng: random.Random, median: float, sigma: float = 0.45) -> float:
    return median * math.exp(rng.gauss(0, sigma))


def volume(team: str, base: int, day_index: int, days: int, weekday: int) -> float:
    v = base * (0.35 if weekday >= 5 else 1.0)              # quiet weekends
    if team == "demo-marketing":
        v *= 1 + 4.0 * day_index / days                     # a launch: steady growth
    if team == "demo-support" and day_index == days - 12:
        v *= 4.0                                            # an incident spike
    return v


def make_rows(rng: random.Random, registry: ModelRegistry, team: str, base: int,
              features: list[str], start: datetime, now: datetime, days: int,
              preview_chars: int):
    logs, verifications, misses = [], [], []
    for day_index in range(days + 1):
        day_start = start + timedelta(days=day_index)
        if day_start > now:
            break
        fraction = min(1.0, (now - day_start).total_seconds() / 86400)
        expected = volume(team, base, day_index, days, day_start.weekday()) * fraction
        count = max(0, round(rng.gauss(expected, math.sqrt(expected or 1))))
        for _ in range(count):
            created = day_start + timedelta(seconds=rng.uniform(0, 86400 * fraction))
            feature = rng.choice(features)
            template, true_tier, tin, tout = rng.choice(TEMPLATES[feature])
            text = template.format(n=rng.randint(1, 9999))
            logs.append(one_request(rng, registry, team, feature, text, true_tier, tin, tout,
                                    created, preview_chars, verifications, misses))
    return logs, verifications, misses


def one_request(rng, registry, team, feature, text, true_tier, tin, tout, created,
                preview_chars, verifications, misses) -> RequestLog:
    source = rng.choices(["routed", "explicit", "pinned"], [0.92, 0.05, 0.03])[0]
    tier = true_tier
    if source == "routed":                                   # the classifier isn't perfect
        roll = rng.random()
        if roll < 0.10 and tier > 1:
            tier -= 1                                        # under-routed (the risky error)
        elif roll > 0.95 and tier < 3:
            tier += 1                                        # over-routed (the wasteful one)
    priority = rng.choices(["low", "normal", "high", "critical"], [0.1, 0.7, 0.15, 0.05])[0]
    confidence = rng.choice([0.5, 0.65, 0.85, 0.9])
    pre = source == "routed" and priority in ("high", "critical") and confidence < 0.6 and tier < 3
    tier += 1 if pre else 0
    downgraded = source == "routed" and tier > 1 and rng.random() < 0.02
    tier -= 1 if downgraded else 0
    escalated = source == "routed" and tier < 3 and rng.random() < 0.03

    status, error_code, override = "success", None, None
    roll = rng.random()
    if roll < 0.010:
        status, error_code = "budget_blocked", "budget_exceeded"
    elif roll < 0.017:
        status, error_code = "provider_error", "provider_error"
    if priority in ("high", "critical") and rng.random() < 0.03:
        override = "incident response"

    input_tokens = int(lognormal(rng, tin))
    output_tokens = int(lognormal(rng, tout))
    served = registry.get(TIER_MODELS[min(tier + (1 if escalated else 0), 3)])
    cost = baseline = Decimal(0)
    latency = None
    if status == "success":
        cost = served.cost(input_tokens, output_tokens)
        if escalated:                                        # the failed cheap attempt is paid too
            cost += registry.get(TIER_MODELS[tier]).cost(input_tokens, output_tokens // 4)
        baseline = registry.get(BASELINE_MODEL).cost(input_tokens, output_tokens)
        latency = lognormal(rng, {1: 700, 2: 1300, 3: 2600}[served.tier], 0.35)
        if rng.random() < 0.04:
            latency *= rng.uniform(3, 8)                     # the tail: slow upstream calls
    final_tier = served.tier if status == "success" else tier

    request_id = uuid.uuid4()
    row = RequestLog(
        id=request_id, created_at=created, team_id=team, feature=feature, priority=priority,
        model=served.name, provider=served.provider, input_tokens=input_tokens if cost else 0,
        output_tokens=output_tokens if cost else 0,
        estimated_cost_usd=served.worst_case_cost(input_tokens, output_tokens * 2),
        cost_usd=quantize_usd(cost), latency_ms=round(latency, 2) if latency else None,
        status=status, error_code=error_code, override_reason=override,
        routed_tier=final_tier, route_source=source,
        classifier_confidence=confidence if source == "routed" else None,
        baseline_cost_usd=baseline if status == "success" else None, escalated=escalated,
        pre_escalated=pre, downgraded=downgraded, prompt_fingerprint=fingerprint_text(text),
        prompt_preview=text[:preview_chars] if preview_chars else None,
        meta={"demo": True, "routing": {"source": source, "final_tier": final_tier}})

    # ~12% of cheap routed answers are verified; under-routed ones fail far more often
    if status == "success" and source == "routed" and final_tier < 3 and not escalated \
            and rng.random() < 0.12:
        fail_p = 0.6 if final_tier < true_tier else (0.14 if final_tier == 1 else 0.07)
        roll = rng.random()
        verdict = "inconclusive" if roll < 0.02 else ("fail" if roll < 0.02 + fail_p else "pass")
        reference = registry.get(BASELINE_MODEL)
        verifications.append(Verification(
            request_id=request_id, created_at=created + timedelta(seconds=rng.uniform(5, 300)),
            team_id=team, feature=feature, model=served.name, tier=final_tier,
            routing_source=source, classifier_confidence=confidence,
            reference_model=reference.name, judge="similarity-sequence", verdict=verdict,
            score=round(rng.uniform(0.85, 1.0) if verdict == "pass" else rng.uniform(0.1, 0.7), 4),
            reason="demo data", verification_cost_usd=reference.cost(input_tokens, output_tokens),
            attempts=1, meta={"demo": True, "budget": {"team_id": "demo-verifier",
                                                       "feature": "demo-verification"}}))
        if verdict == "fail":
            misses.append(RoutingMiss(
                request_id=request_id, created_at=created + timedelta(seconds=300),
                team_id=team, feature=feature, prompt=text, chosen_model=served.name,
                chosen_tier=final_tier, better_model=reference.name, better_tier=3,
                reason="demo data", classifier_confidence=confidence,
                classifier_features={"demo": True}))
    return row


async def reset(session_factory) -> int:
    teams = list(DEMO_TEAMS)
    async with session_factory() as s:
        deleted = 0
        for model in (RoutingMiss, Verification, BudgetAlert, RequestLog):
            result = await s.execute(delete(model).where(
                (model.scope_id.in_(teams)) if model is BudgetAlert else model.team_id.in_(teams)))
            deleted += result.rowcount or 0
        await s.execute(delete(BudgetPolicy).where(BudgetPolicy.scope == "team",
                                                   BudgetPolicy.scope_id.in_(teams)))
        await s.commit()
    return deleted


def alerts_for(team: str, limit: Decimal, logs: list[RequestLog], now: datetime) -> list:
    """Monthly 80% / 100% crossings this month, as the live gateway would have recorded."""
    month = month_period(now)
    spent, alerts, crossed = Decimal(0), [], set()
    for row in sorted((r for r in logs if r.created_at >= month.start), key=lambda r: r.created_at):
        spent += row.cost_usd
        for threshold in (Decimal("0.80"), Decimal("1.00")):
            if threshold not in crossed and spent >= limit * threshold:
                crossed.add(threshold)
                alerts.append(BudgetAlert(created_at=row.created_at, scope="team", scope_id=team,
                                          period="month", period_key=month.key,
                                          threshold=threshold, projected_usd=quantize_usd(spent),
                                          limit_usd=limit, request_id=row.id))
    return alerts


async def seed(session_factory, days: int, seed_value: int, now: datetime,
               preview_chars: int) -> dict:
    rng = random.Random(seed_value)
    registry = ModelRegistry(settings.model_registry_path)
    start = datetime.combine((now - timedelta(days=days)).date(), datetime.min.time(), tzinfo=UTC)
    month = month_period(now)
    elapsed = max((now - month.start).total_seconds() / 86400, 1.0)
    days_in_month = (month.resets_at - month.start).days
    summary = {}
    await reset(session_factory)
    async with session_factory() as s:
        for team, (base, limit, features) in DEMO_TEAMS.items():
            logs, verifications, misses = make_rows(rng, registry, team, base, features, start,
                                                    now, days, preview_chars)
            if limit is None:
                # The launch team: a limit its run-rate will overshoot by ~25% (at risk)
                mtd = sum((r.cost_usd for r in logs if r.created_at >= month.start), Decimal(0))
                limit = quantize_usd(max(mtd / Decimal(str(elapsed)) * days_in_month / Decimal("1.25"),
                                         Decimal("1.00")))
            await upsert_policy(s, "team", team, None, limit)
            alerts = alerts_for(team, limit, logs, now)
            for i in range(0, len(logs), 2000):
                s.add_all(logs[i:i + 2000])
                await s.flush()
            s.add_all(verifications + misses + alerts)
            await s.flush()
            summary[team] = {"requests": len(logs), "verifications": len(verifications),
                             "misses": len(misses), "alerts": len(alerts),
                             "monthly_limit_usd": str(limit),
                             "cost_usd": str(sum((r.cost_usd for r in logs), Decimal(0)))}
        await s.commit()
    return summary


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed (or remove) dashboard demo data.")
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reset", action="store_true", help="delete the demo data and exit")
    args = parser.parse_args(argv)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        if args.reset:
            print(f"Deleted {await reset(session_factory)} demo row(s) for {', '.join(DEMO_TEAMS)}")
            return 0
        result = await seed(session_factory, args.days, args.seed, datetime.now(UTC),
                            preview_chars=120)
    finally:
        await engine.dispose()
    print(f"{'team':<16}{'requests':>9}{'verified':>9}{'misses':>8}{'alerts':>7}"
          f"{'cost':>14}{'monthly limit':>15}")
    for team, r in result.items():
        print(f"{team:<16}{r['requests']:>9}{r['verifications']:>9}{r['misses']:>8}"
              f"{r['alerts']:>7}{'$' + r['cost_usd'][:9]:>14}{'$' + r['monthly_limit_usd']:>15}")
    print("\nDemo data seeded. Redis budget counters were not touched: restart the API or "
          "POST /v1/budgets/reconcile to include it in real-time budgets.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
