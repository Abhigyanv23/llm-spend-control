import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.audit import AuditRecord
from app.budgets import BudgetService, Reservation
from app.errors import ContextTooLongError, GatewayError
from app.providers.base import ProviderAdapter
from app.registry import ModelRegistry, ModelSpec
from app.schemas import ChatRequest, ChatResponse, Usage
from app.tokens import estimate_input_tokens

logger = logging.getLogger(__name__)

MAX_OVERRIDE_REASON_LENGTH = 500


@dataclass
class GatewayResult:
    """What handle() returns on success. The HTTP layer decides how to deliver each part."""
    response: ChatResponse
    audit: AuditRecord
    headers: dict[str, str] = field(default_factory=dict)


class Gateway:
    """Core pipeline:
        resolve model -> context check -> estimate cost -> reserve (may warn/block)
        -> provider call -> settle -> (caller writes audit log) -> respond
    Phase 3 adds routing in front of this."""

    def __init__(self, registry: ModelRegistry, adapters: dict[str, ProviderAdapter],
                 budgets: BudgetService):
        # Dependencies are injected, not constructed here: tests pass fakes, prod passes real ones
        self.registry = registry
        self.adapters = adapters
        self.budgets = budgets

    def resolve_model(self, request: ChatRequest) -> ModelSpec:
        name = request.model or self.registry.default_model
        spec = self.registry.get(name)
        if spec.provider not in self.adapters:
            raise GatewayError(f"No adapter for provider '{spec.provider}'",
                               status_code=500, code="no_adapter")
        return spec

    def check_context(self, request: ChatRequest, spec: ModelSpec) -> None:
        estimated = estimate_input_tokens(request.messages) + request.max_tokens
        if estimated > spec.max_context:
            raise ContextTooLongError(spec.name, estimated, spec.max_context)

    async def handle(self, request: ChatRequest,
                     override_reason: str | None = None) -> GatewayResult:
        request_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        override_reason = _clean_reason(override_reason)
        record = AuditRecord(request_id=request_id, created_at=now, team_id=request.team_id,
                             feature=request.feature, priority=request.priority.value,
                             model=request.model or self.registry.default_model)
        reservation: Reservation | None = None
        settled = False

        try:
            spec = self.resolve_model(request)
            record.model, record.provider = spec.name, spec.provider
            self.check_context(request, spec)

            # 1. Worst case: estimated input + ALL of max_tokens at the output price
            estimate = spec.worst_case_cost(estimate_input_tokens(request.messages),
                                            request.max_tokens)
            record.estimated_cost_usd = estimate

            # 2. Reserve BEFORE spending. Raises 402/503 if the request must not run.
            reservation = await self.budgets.reserve(
                request_id=request_id, team_id=request.team_id, feature=request.feature,
                priority=request.priority, estimate_usd=estimate,
                override_reason=override_reason, now=now)

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

            record.input_tokens, record.output_tokens = result.input_tokens, result.output_tokens
            record.cost_usd = cost
            record.override_reason = reservation.override_reason
            budget_meta = reservation.metadata()
            record.metadata = {"raw_model": result.raw_model, **result.metadata, **budget_meta}

            response = ChatResponse(
                request_id=request_id, model=spec.name, provider=spec.provider,
                output=result.output,
                usage=Usage(input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens),
                cost_usd=cost, latency_ms=record.latency_ms,
                metadata={**result.metadata, "raw_model": result.raw_model,
                          "team_id": request.team_id, "feature": request.feature,
                          "priority": request.priority.value, **budget_meta},
            )
            headers = {}
            if warning := reservation.warning_header():
                headers["X-Budget-Warning"] = warning
            return GatewayResult(response=response, audit=record, headers=headers)

        except GatewayError as exc:
            record.status, record.error_code = exc.audit_status, exc.code
            record.metadata = {"error": exc.message[:500], "details": dict(exc.extra)}
            exc.extra.setdefault("request_id", request_id)
            exc.audit_record = record           # the error handler writes it
            raise
        except Exception as exc:
            logger.exception("Unhandled error in gateway request_id=%s", request_id)
            err = GatewayError("Internal gateway error", status_code=500,
                               code="internal_error", extra={"request_id": request_id})
            record.status, record.error_code = "internal_error", "internal_error"
            record.metadata = {"error": f"{type(exc).__name__}: {exc}"[:500]}
            err.audit_record = record
            raise err from exc
        finally:
            # The guarantee: a hold can NEVER leak. Any exception after reserve (provider
            # error, timeout, bug, cancellation) releases it. shield() lets the release
            # finish even if this task is being cancelled.
            if reservation is not None and not settled:
                await asyncio.shield(self.budgets.release(reservation))


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    reason = reason.strip()
    return reason[:MAX_OVERRIDE_REASON_LENGTH] or None
