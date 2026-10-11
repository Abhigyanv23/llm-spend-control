import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.audit import AuditRecord
from app.budgets import BudgetService, Reservation
from app.errors import BudgetExceededError, ContextTooLongError, GatewayError, OverrideRequiredError
from app.fingerprint import prompt_fingerprint, prompt_preview
from app.money import usd_str
from app.providers.base import ProviderAdapter, ProviderResult
from app.quality import QualityConfig, decide_sampling
from app.quality.escalation import apply_pre_call_escalation, check_answer
from app.quality.jobs import VerificationJob
from app.registry import ModelRegistry, ModelSpec
from app.routing import RouteDecision, Router
from app.schemas import ChatRequest, ChatResponse, Usage
from app.tokens import estimate_input_tokens

logger = logging.getLogger(__name__)

MAX_OVERRIDE_REASON_LENGTH = 500
# Budget decisions that a cheaper model might avoid (a 503 budget_unavailable is not one)
BUDGET_BLOCK_ERRORS = (BudgetExceededError, OverrideRequiredError)


@dataclass
class GatewayResult:
    """What handle() returns on success. The HTTP layer decides how to deliver each part."""
    response: ChatResponse
    audit: AuditRecord
    headers: dict[str, str] = field(default_factory=dict)
    verification_job: VerificationJob | None = None     # set when sampled for verification


class Gateway:
    """Core pipeline:
        route (tier + model) -> context check -> estimate cost -> reserve (may warn/block;
        on a block, downgrade to a cheaper allowed model if routing permits)
        -> provider call -> settle -> (caller writes audit log) -> respond
    """

    def __init__(self, registry: ModelRegistry, adapters: dict[str, ProviderAdapter],
                 budgets: BudgetService, router: Router | None = None,
                 quality: QualityConfig | None = None):
        # Dependencies are injected, not constructed here: tests pass fakes, prod passes real ones
        self.registry = registry
        self.adapters = adapters
        self.budgets = budgets
        self.router = router
        self.quality = quality

    def route(self, request: ChatRequest) -> RouteDecision:
        if self.router is not None:
            return self.router.route(request)
        # No router (Phase 2 behaviour): explicit model or the registry default
        spec = self.registry.get(request.model or self.registry.default_model)
        return RouteDecision(model=spec.name, tier=spec.tier,
                             source="explicit" if request.model else "default",
                             min_tier=spec.tier, max_tier=spec.tier)

    def resolve_model(self, name: str) -> ModelSpec:
        spec = self.registry.get(name)
        if spec.provider not in self.adapters:
            raise GatewayError(f"No adapter for provider '{spec.provider}'",
                               status_code=500, code="no_adapter")
        return spec

    def check_context(self, request: ChatRequest, spec: ModelSpec) -> None:
        estimated = estimate_input_tokens(request.messages) + request.max_tokens
        if estimated > spec.max_context:
            raise ContextTooLongError(spec.name, estimated, spec.max_context)

    def baseline_cost(self, input_tokens: int, output_tokens: int) -> tuple[str, Decimal] | None:
        """Counterfactual: the same tokens priced on the strongest model."""
        if self.router is None:
            return None
        baseline = self.router.baseline
        return baseline.name, baseline.cost(input_tokens, output_tokens)

    async def _reserve_with_downgrade(self, request: ChatRequest, decision: RouteDecision,
                                      request_id: str, override_reason: str | None,
                                      now: datetime, record: AuditRecord,
                                      downgrades: list[dict]) -> tuple[ModelSpec, Reservation]:
        """Reserve for the routed model; if a budget blocks it, try each cheaper fallback.
        Safe because a blocked reserve holds nothing (the Lua script is all-or-nothing)."""
        candidates = [decision.model, *decision.fallbacks]
        input_tokens = estimate_input_tokens(request.messages)
        for attempt, name in enumerate(candidates):
            spec = self.resolve_model(name)
            record.model, record.provider = spec.name, spec.provider
            self.check_context(request, spec)
            # Worst case: estimated input + ALL of max_tokens at the output price
            estimate = spec.worst_case_cost(input_tokens, request.max_tokens)
            record.estimated_cost_usd = estimate
            try:
                reservation = await self.budgets.reserve(
                    request_id=request_id, team_id=request.team_id, feature=request.feature,
                    priority=request.priority, estimate_usd=estimate,
                    override_reason=override_reason, now=now)
                return spec, reservation
            except BUDGET_BLOCK_ERRORS as exc:
                if attempt == len(candidates) - 1:
                    raise       # nothing cheaper is allowed: the block stands
                downgrades.append({"model": spec.name, "tier": spec.tier,
                                   "blocked_by": exc.code,
                                   "estimated_cost_usd": usd_str(estimate)})
                logger.info("request_id=%s: budget blocked %s (%s); downgrading to %s",
                            request_id, spec.name, exc.code, candidates[attempt + 1])
        raise AssertionError("unreachable")

    async def handle(self, request: ChatRequest,
                     override_reason: str | None = None) -> GatewayResult:
        request_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        override_reason = _clean_reason(override_reason)
        record = AuditRecord(request_id=request_id, created_at=now, team_id=request.team_id,
                             feature=request.feature, priority=request.priority.value,
                             model=request.model,
                             prompt_fingerprint=prompt_fingerprint(request),
                             prompt_preview=self._preview(request))
        reservation: Reservation | None = None
        settled = False
        decision: RouteDecision | None = None
        spec: ModelSpec | None = None
        downgrades: list[dict] = []
        escalation: dict | None = None

        try:
            # 1. Route: explicit model, pinned feature, or classifier tier within bounds
            decision = self.route(request)
            # 1b. Pre-call escalation (Phase 4): an important request the classifier is unsure
            #     about starts one tier higher. Still one call, so no double spend.
            if self.quality is not None:
                decision, pre_call = apply_pre_call_escalation(
                    self.router, self.quality.escalation, request, decision)
                escalation = {"pre_call": pre_call, "attempts": [], "escalated": False}
                record.pre_escalated = bool(pre_call and pre_call.get("applied"))
            record.model = decision.model
            record.route_source = decision.source
            if decision.classification is not None:
                record.classifier_confidence = decision.classification.confidence

            # 2. Reserve BEFORE spending (downgrading if a budget blocks). Raises 402/503.
            spec, reservation = await self._reserve_with_downgrade(
                request, decision, request_id, override_reason, now, record, downgrades)

            # 3. Provider call
            adapter = self.adapters[spec.provider]
            start = time.perf_counter()
            try:
                result = await adapter.complete(request, spec.name)
            finally:
                record.latency_ms = round((time.perf_counter() - start) * 1000, 2)

            # 4. Settle: swap the hold for the actual cost
            cost = spec.cost(result.input_tokens, result.output_tokens)
            await self.budgets.settle(reservation, cost)
            settled = True

            # 5. Post-call checks; on a visible failure, cascade one tier up (Phase 4).
            #    Each attempt is reserved and settled separately: its cost is real.
            esc = _Escalation(spec=spec, result=result)
            if escalation is not None:
                esc.final_check_failed = self._check(request, decision, result)
                escalation["attempts"].append(_attempt_meta(
                    spec, result, cost, record.latency_ms, record.estimated_cost_usd,
                    esc.final_check_failed))
                if esc.final_check_failed:
                    await self._post_call_escalation(request, request_id, now, decision,
                                                     override_reason, esc)
                escalation["attempts"].extend(esc.attempts)
                escalation.update(escalated=esc.escalations > 0, escalations=esc.escalations,
                                  final_check_failed=esc.final_check_failed,
                                  blocked=esc.blocked, note=esc.note,
                                  extra_cost_usd=usd_str(esc.cost))
            final_spec, final = esc.spec, esc.result
            total_cost = cost + esc.cost
            total_in = result.input_tokens + esc.input_tokens
            total_out = result.output_tokens + esc.output_tokens

            # 6. Routing metadata + savings versus the strongest model (net of escalation)
            routing_meta = _routing_metadata(decision, final_spec, downgrades)
            if baseline := self.baseline_cost(final.input_tokens, final.output_tokens):
                baseline_model, baseline_usd = baseline
                routing_meta.update(baseline_model=baseline_model,
                                    baseline_cost_usd=usd_str(baseline_usd),
                                    savings_usd=usd_str(baseline_usd - total_cost))
                record.baseline_cost_usd = baseline_usd

            # 7. Should a stronger model double-check this answer later? Not if the cascade
            #    already replaced it.
            quality_meta, job = self._sample(request, request_id, now, decision, final_spec,
                                             final, escalated=esc.escalations > 0)
            escalation_meta = {"escalation": escalation} if escalation is not None else {}

            # One audit row per request: the returned answer's model, ALL attempts' cost
            record.model, record.provider = final_spec.name, final_spec.provider
            record.routed_tier = final_spec.tier
            record.escalated = esc.escalations > 0
            record.downgraded = bool(downgrades)
            record.input_tokens, record.output_tokens = total_in, total_out
            record.cost_usd = total_cost
            record.estimated_cost_usd = record.estimated_cost_usd + esc.estimate
            record.latency_ms = round(record.latency_ms + esc.latency_ms, 2)
            record.override_reason = reservation.override_reason
            budget_meta = reservation.metadata()
            record.metadata = {"raw_model": final.raw_model, **final.metadata, **budget_meta,
                               "routing": routing_meta, **quality_meta, **escalation_meta}

            response = ChatResponse(
                request_id=request_id, model=final_spec.name, provider=final_spec.provider,
                output=final.output,
                usage=Usage(input_tokens=total_in, output_tokens=total_out),
                cost_usd=total_cost, latency_ms=record.latency_ms,
                metadata={**final.metadata, "raw_model": final.raw_model,
                          "team_id": request.team_id, "feature": request.feature,
                          "priority": request.priority.value, **budget_meta,
                          "routing": routing_meta, **quality_meta, **escalation_meta},
            )
            headers = {}
            if warning := reservation.warning_header():
                headers["X-Budget-Warning"] = warning
            return GatewayResult(response=response, audit=record, headers=headers,
                                 verification_job=job)

        except GatewayError as exc:
            record.status, record.error_code = exc.audit_status, exc.code
            record.routed_tier = spec.tier if spec is not None else None
            record.downgraded = bool(downgrades)
            record.metadata = {"error": exc.message[:500], "details": dict(exc.extra),
                               "routing": _routing_metadata(decision, spec, downgrades)}
            exc.extra.setdefault("request_id", request_id)
            exc.audit_record = record           # the error handler writes it
            raise
        except Exception as exc:
            logger.exception("Unhandled error in gateway request_id=%s", request_id)
            err = GatewayError("Internal gateway error", status_code=500,
                               code="internal_error", extra={"request_id": request_id})
            record.status, record.error_code = "internal_error", "internal_error"
            record.metadata = {"error": f"{type(exc).__name__}: {exc}"[:500],
                               "routing": _routing_metadata(decision, spec, downgrades)}
            err.audit_record = record
            raise err from exc
        finally:
            # The guarantee: a hold can NEVER leak. Any exception after reserve (provider
            # error, timeout, bug, cancellation) releases it. shield() lets the release
            # finish even if this task is being cancelled.
            if reservation is not None and not settled:
                await asyncio.shield(self.budgets.release(reservation))

    def _preview(self, request: ChatRequest) -> str | None:
        """A short prompt preview for analytics, only if privacy settings allow it."""
        if self.quality is None:
            return None
        privacy = self.quality.privacy
        if not privacy.store_prompts or privacy.prompt_preview_chars == 0:
            return None
        return prompt_preview(request, privacy.prompt_preview_chars)

    def _check(self, request: ChatRequest, decision: RouteDecision,
               result: ProviderResult) -> str | None:
        return check_answer(self.quality.escalation, request, result.output,
                            result.metadata.get("finish_reason"), decision.classification)

    async def _post_call_escalation(self, request: ChatRequest, request_id: str, now: datetime,
                                    decision: RouteDecision, override_reason: str | None,
                                    esc: "_Escalation") -> None:
        """Retry on the next tier up while the answer fails a check and escalations remain.
        Never fails the request: if escalating is impossible, blocked by budget or errors,
        the user gets the answer we already have, with a note saying why."""
        cfg = self.quality.escalation
        input_estimate = estimate_input_tokens(request.messages)
        while esc.final_check_failed:
            if not cfg.enabled:
                esc.note = "escalation disabled"
                return
            if esc.escalations >= cfg.max_escalations:
                esc.note = f"max_escalations ({cfg.max_escalations}) reached"
                return
            if decision.source != "routed" or self.router is None:
                esc.note = (f"not escalated: the model was chosen by '{decision.source}', "
                            f"not the router")
                return
            if esc.spec.tier >= decision.max_tier:
                esc.note = ("not escalated: already at the highest allowed tier "
                            f"({decision.max_tier})")
                return
            nxt = self.router.pick_from_tier(request, esc.spec.tier + 1, decision.max_tier)
            if nxt is None:
                esc.note = "not escalated: no usable model in a higher tier"
                return

            estimate = nxt.worst_case_cost(input_estimate, request.max_tokens)
            try:
                reservation = await self.budgets.reserve(
                    request_id=request_id, team_id=request.team_id, feature=request.feature,
                    priority=request.priority, estimate_usd=estimate,
                    override_reason=override_reason, now=now)
            except BUDGET_BLOCK_ERRORS as exc:
                esc.blocked = {"model": nxt.name, "tier": nxt.tier, "blocked_by": exc.code,
                               "estimated_cost_usd": usd_str(estimate)}
                esc.note = "escalation blocked by budget: returning the original answer"
                return

            previous = esc.spec.name
            settled = False
            start = time.perf_counter()
            try:
                new = await self.adapters[nxt.provider].complete(request, nxt.name)
                new_cost = nxt.cost(new.input_tokens, new.output_tokens)
                await self.budgets.settle(reservation, new_cost)
                settled = True
            except GatewayError as exc:
                esc.attempts.append({"model": nxt.name, "tier": nxt.tier, "error": exc.code,
                                     "cost_usd": usd_str(0),
                                     "estimated_cost_usd": usd_str(estimate)})
                esc.note = (f"escalation to {nxt.name} failed ({exc.code}): "
                            f"returning the original answer")
                return
            finally:
                latency = round((time.perf_counter() - start) * 1000, 2)
                esc.latency_ms += latency
                if not settled:
                    await asyncio.shield(self.budgets.release(reservation))

            trigger = esc.final_check_failed
            esc.escalations += 1
            esc.final_check_failed = self._check(request, decision, new)
            esc.attempts.append({**_attempt_meta(nxt, new, new_cost, latency, estimate,
                                                 esc.final_check_failed),
                                 "escalated_because": trigger})
            esc.cost += new_cost
            esc.estimate += estimate
            esc.input_tokens += new.input_tokens
            esc.output_tokens += new.output_tokens
            esc.spec, esc.result = nxt, new
            logger.info("request_id=%s: escalated %s -> %s (%s)", request_id, previous,
                        nxt.name, trigger)

    def _sample(self, request: ChatRequest, request_id: str, now: datetime,
                decision: RouteDecision, spec: ModelSpec, result: ProviderResult,
                escalated: bool = False) -> tuple[dict, VerificationJob | None]:
        """Deterministic sampling decision + the self-contained job for the worker."""
        if self.quality is None:
            return {}, None
        classification = decision.classification
        sample = decide_sampling(
            self.quality.sampling, request_id=request_id, source=decision.source,
            final_tier=spec.tier,
            confidence=classification.confidence if classification else None,
            escalated=escalated, verify_enabled=self.quality.verify_enabled)
        meta = {"quality": {"sampling": sample.to_dict()}}
        if not sample.sampled:
            return meta, None
        job = VerificationJob(
            request_id=request_id, created_at=now.isoformat().replace("+00:00", "Z"),
            team_id=request.team_id, feature=request.feature, priority=request.priority.value,
            messages=[m.model_dump() for m in request.messages], output=result.output,
            model=spec.name, tier=spec.tier, routing_source=decision.source,
            classifier_confidence=classification.confidence if classification else None,
            classifier_features=classification.features.to_dict() if classification else {},
            classifier_reasons=list(classification.reasons) if classification else [],
            sample_rate=sample.rate)
        return meta, job


@dataclass
class _Escalation:
    """Running state of the post-call cascade: the answer we would return right now."""
    spec: ModelSpec
    result: ProviderResult
    attempts: list[dict] = field(default_factory=list)    # escalation attempts only
    escalations: int = 0
    final_check_failed: str | None = None
    blocked: dict | None = None
    note: str | None = None
    cost: Decimal = Decimal(0)
    estimate: Decimal = Decimal(0)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


def _attempt_meta(spec: ModelSpec, result: ProviderResult, cost: Decimal, latency_ms: float,
                  estimate: Decimal, check_failed: str | None) -> dict:
    return {"model": spec.name, "tier": spec.tier, "cost_usd": usd_str(cost),
            "estimated_cost_usd": usd_str(estimate), "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "latency_ms": latency_ms,
            "finish_reason": result.metadata.get("finish_reason"), "check_failed": check_failed}


def _routing_metadata(decision: RouteDecision | None, spec: ModelSpec | None,
                      downgrades: list[dict]) -> dict | None:
    if decision is None:
        return None
    meta = decision.metadata()
    if spec is not None:
        meta["final_model"], meta["final_tier"] = spec.name, spec.tier
    meta["downgrades"] = list(downgrades)
    return meta


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    reason = reason.strip()
    return reason[:MAX_OVERRIDE_REASON_LENGTH] or None
