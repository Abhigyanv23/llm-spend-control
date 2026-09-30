import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.audit import AuditRecord
from app.budgets import BudgetService, Reservation
from app.errors import (BudgetExceededError, ContextTooLongError, GatewayError,
                        OverrideRequiredError)
from app.money import usd_str
from app.providers.base import ProviderAdapter
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


class Gateway:
    """Core pipeline:
        route (tier + model) -> context check -> estimate cost -> reserve (may warn/block;
        on a block, downgrade to a cheaper allowed model if routing permits)
        -> provider call -> settle -> (caller writes audit log) -> respond
    """

    def __init__(self, registry: ModelRegistry, adapters: dict[str, ProviderAdapter],
                 budgets: BudgetService, router: Router | None = None):
        # Dependencies are injected, not constructed here: tests pass fakes, prod passes real ones
        self.registry = registry
        self.adapters = adapters
        self.budgets = budgets
        self.router = router

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
                             model=request.model)
        reservation: Reservation | None = None
        settled = False
        decision: RouteDecision | None = None
        spec: ModelSpec | None = None
        downgrades: list[dict] = []

        try:
            # 1. Route: explicit model, pinned feature, or classifier tier within bounds
            decision = self.route(request)
            record.model = decision.model

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

            # 5. Routing metadata + savings versus the strongest model
            routing_meta = _routing_metadata(decision, spec, downgrades)
            if baseline := self.baseline_cost(result.input_tokens, result.output_tokens):
                baseline_model, baseline_usd = baseline
                routing_meta.update(baseline_model=baseline_model,
                                    baseline_cost_usd=usd_str(baseline_usd),
                                    savings_usd=usd_str(baseline_usd - cost))

            record.input_tokens, record.output_tokens = result.input_tokens, result.output_tokens
            record.cost_usd = cost
            record.override_reason = reservation.override_reason
            budget_meta = reservation.metadata()
            record.metadata = {"raw_model": result.raw_model, **result.metadata, **budget_meta,
                               "routing": routing_meta}

            response = ChatResponse(
                request_id=request_id, model=spec.name, provider=spec.provider,
                output=result.output,
                usage=Usage(input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens),
                cost_usd=cost, latency_ms=record.latency_ms,
                metadata={**result.metadata, "raw_model": result.raw_model,
                          "team_id": request.team_id, "feature": request.feature,
                          "priority": request.priority.value, **budget_meta,
                          "routing": routing_meta},
            )
            headers = {}
            if warning := reservation.warning_header():
                headers["X-Budget-Warning"] = warning
            return GatewayResult(response=response, audit=record, headers=headers)

        except GatewayError as exc:
            record.status, record.error_code = exc.audit_status, exc.code
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