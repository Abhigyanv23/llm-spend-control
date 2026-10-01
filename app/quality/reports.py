"""Read side of Phase 4: quality summary, routing-miss listing, and export.

The headline numbers answer "is cheap routing safe, and is it still saving money?":
  miss_rate           fail / (pass + fail) among verified cheap answers, with a 95% margin
  miss_rate_weighted  the same, re-weighted by 1/sample_rate (low-confidence routes are
                      over-sampled on purpose, so the raw rate overstates misses)
  net_savings_usd     routing savings (already net of escalation) minus verification spend
"""
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Numeric, cast, func, select, true
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import VERIFICATION_VERDICTS, RequestLog, RoutingMiss, Verification
from app.money import quantize_usd, usd_str

PROMPT_PREVIEW_CHARS = 200


@dataclass
class QualityFilter:
    start: datetime | None = None        # inclusive
    end: datetime | None = None          # exclusive
    team_id: str | None = None
    feature: str | None = None

    def apply(self, model) -> list:
        conds = []
        if self.start is not None:
            conds.append(model.created_at >= self.start)
        if self.end is not None:
            conds.append(model.created_at < self.end)
        if self.team_id is not None:
            conds.append(model.team_id == self.team_id)
        if self.feature is not None:
            conds.append(model.feature == self.feature)
        return conds


def _rate(numerator: int | float, denominator: int | float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _margin_95(p: float | None, n: int) -> float | None:
    """Normal-approximation 95% margin of error of a proportion."""
    if p is None or n == 0:
        return None
    return round(1.96 * math.sqrt(p * (1 - p) / n), 4)


def _json_money(column) -> object:
    """SUM over a money value stored as a string inside JSON metadata."""
    return func.coalesce(func.sum(cast(column.as_string(), Numeric(14, 8))), 0)


async def quality_summary(session_factory: async_sessionmaker, f: QualityFilter) -> dict:
    vc, mc = f.apply(Verification), f.apply(RoutingMiss)
    lc = [*f.apply(RequestLog), RequestLog.status == "success"]
    meta = RequestLog.meta
    async with session_factory() as s:
        verdict_rows = (await s.execute(
            select(Verification.verdict, func.count(),
                   func.coalesce(func.sum(Verification.verification_cost_usd), 0))
            .where(*vc).group_by(Verification.verdict))).all()
        model_rows = (await s.execute(
            select(Verification.model, Verification.verdict, func.count())
            .where(*vc, Verification.verdict.in_(("pass", "fail")))
            .group_by(Verification.model, Verification.verdict))).all()
        weight_rows = (await s.execute(
            select(Verification.verdict, Verification.meta["sample_rate"].as_float())
            .where(*vc, Verification.verdict.in_(("pass", "fail"))))).all()
        miss_by_model = (await s.execute(
            select(RoutingMiss.chosen_model, func.count()).where(*mc)
            .group_by(RoutingMiss.chosen_model))).all()
        miss_by_feature = (await s.execute(
            select(RoutingMiss.feature, func.count()).where(*mc)
            .group_by(RoutingMiss.feature))).all()
        req_count, req_cost, gross_savings = (await s.execute(
            select(func.count(), func.coalesce(func.sum(RequestLog.cost_usd), 0),
                   _json_money(meta["routing"]["savings_usd"])).where(*lc))).one()
        sampled = await s.scalar(select(func.count()).where(
            *lc, meta["quality"]["sampling"]["sampled"].as_boolean() == true()))
        escalated, escalation_cost = (await s.execute(
            select(func.count(), _json_money(meta["escalation"]["extra_cost_usd"]))
            .where(*lc, meta["escalation"]["escalated"].as_boolean() == true()))).one()
        pre_call = await s.scalar(select(func.count()).where(
            *lc, meta["escalation"]["pre_call"]["applied"].as_boolean() == true()))

    counts = {v: 0 for v in VERIFICATION_VERDICTS}
    verification_cost = Decimal(0)
    for verdict, count, cost in verdict_rows:
        counts[verdict] = int(count)
        verification_cost += quantize_usd(cost)
    verified = sum(counts.values())
    judged = counts["pass"] + counts["fail"]
    miss_rate = _rate(counts["fail"], judged)

    # Stratified estimate: each judged answer stands for 1/sample_rate answers
    weights = {"pass": 0.0, "fail": 0.0}
    for verdict, rate in weight_rows:
        weights[verdict] += 1 / rate if rate else 1.0
    weighted = _rate(weights["fail"], weights["pass"] + weights["fail"])

    per_model: dict[str, dict] = {}
    for model, verdict, count in model_rows:
        per_model.setdefault(model, {"pass": 0, "fail": 0})[verdict] = int(count)
    by_model = [{"model": m, "judged": c["pass"] + c["fail"], "fail": c["fail"],
                 "miss_rate": _rate(c["fail"], c["pass"] + c["fail"])}
                for m, c in sorted(per_model.items())]

    request_cost = quantize_usd(req_cost)
    gross = quantize_usd(gross_savings)
    return {
        "verification": {
            "verified": verified, **counts,
            "rates": {v: _rate(counts[v], verified) for v in VERIFICATION_VERDICTS},
            "miss_rate": miss_rate,
            "miss_rate_margin_95": _margin_95(miss_rate, judged),
            "miss_rate_weighted": weighted,
            "by_model": by_model,
            "verification_cost_usd": usd_str(verification_cost),
        },
        "misses": {
            "total": sum(int(c) for _, c in miss_by_model),
            "by_model": [{"model": m, "count": int(c)} for m, c in sorted(miss_by_model)],
            "by_feature": [{"feature": f_, "count": int(c)} for f_, c in sorted(miss_by_feature)],
        },
        "requests": {
            "successful": int(req_count), "sampled": int(sampled or 0),
            "sample_rate": _rate(int(sampled or 0), int(req_count)),
        },
        "escalation": {
            "pre_call_escalations": int(pre_call or 0),
            "post_call_escalations": int(escalated),
            "escalation_rate": _rate(int(escalated), int(req_count)),
            "escalation_extra_cost_usd": usd_str(quantize_usd(escalation_cost)),
        },
        "costs": {
            "requests_cost_usd": usd_str(request_cost),
            "verification_cost_usd": usd_str(verification_cost),
            "verification_overhead_pct": (round(float(verification_cost / request_cost * 100), 2)
                                          if request_cost else None),
            "gross_savings_usd": usd_str(gross),
            "net_savings_usd": usd_str(gross - verification_cost),
        },
    }


def _miss_dict(m: RoutingMiss, full_prompt: bool) -> dict:
    """Listings show a short preview; exports carry the full stored prompt."""
    d = {"request_id": str(m.request_id), "created_at": m.created_at,
         "team_id": m.team_id, "feature": m.feature,
         "prompt_chars": len(m.prompt) if m.prompt is not None else None,
         "chosen_model": m.chosen_model, "chosen_tier": m.chosen_tier,
         "better_model": m.better_model, "better_tier": m.better_tier, "reason": m.reason,
         "classifier_confidence": m.classifier_confidence,
         "classifier_features": m.classifier_features}
    if full_prompt:
        d["prompt"] = m.prompt
    else:
        d["prompt_preview"] = None if m.prompt is None else m.prompt[:PROMPT_PREVIEW_CHARS]
    return d


async def list_misses(session_factory: async_sessionmaker, f: QualityFilter,
                      model: str | None, limit: int, offset: int) -> dict:
    conds = f.apply(RoutingMiss)
    if model is not None:
        conds.append(RoutingMiss.chosen_model == model)
    async with session_factory() as s:
        total = await s.scalar(select(func.count()).select_from(RoutingMiss).where(*conds))
        rows = (await s.scalars(select(RoutingMiss).where(*conds)
                                .order_by(RoutingMiss.created_at.desc(), RoutingMiss.id)
                                .limit(limit).offset(offset))).all()
    return {"items": [_miss_dict(m, full_prompt=False) for m in rows], "total": int(total),
            "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}


async def iter_misses(session_factory: async_sessionmaker, f: QualityFilter,
                      batch_size: int = 500):
    """Yield every matching miss (full prompt), oldest first, in id-keyed batches (keyset
    pagination: stays fast however large the table grows)."""
    last_id = 0
    while True:
        async with session_factory() as s:
            rows = (await s.scalars(select(RoutingMiss)
                                    .where(*f.apply(RoutingMiss), RoutingMiss.id > last_id)
                                    .order_by(RoutingMiss.id).limit(batch_size))).all()
        if not rows:
            return
        for m in rows:
            yield _miss_dict(m, full_prompt=True)
        last_id = rows[-1].id
