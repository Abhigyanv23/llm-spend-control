import time
import uuid

from app.errors import ContextTooLongError, GatewayError
from app.providers.base import ProviderAdapter
from app.providers.mock_adapter import estimate_tokens
from app.registry import ModelRegistry, ModelSpec
from app.schemas import ChatRequest, ChatResponse, Usage


class Gateway:
    """Core pipeline. Phase 2 adds budget checks here, Phase 3 adds routing."""

    def __init__(self, registry: ModelRegistry, adapters: dict[str, ProviderAdapter]):
        self.registry = registry
        self.adapters = adapters

    def resolve_model(self, request: ChatRequest) -> ModelSpec:
        name = request.model or self.registry.default_model
        spec = self.registry.get(name)
        if spec.provider not in self.adapters:
            raise GatewayError(f"No adapter for provider '{spec.provider}'",
                               status_code=500, code="no_adapter")
        return spec

    def check_context(self, request: ChatRequest, spec: ModelSpec) -> None:
        prompt = " ".join(m.content for m in request.messages)
        estimated = estimate_tokens(prompt) + request.max_tokens
        if estimated > spec.max_context:
            raise ContextTooLongError(spec.name, estimated, spec.max_context)

    async def handle(self, request: ChatRequest) -> ChatResponse:
        request_id = str(uuid.uuid4())
        spec = self.resolve_model(request)
        self.check_context(request, spec)

        adapter = self.adapters[spec.provider]
        start = time.perf_counter()
        result = await adapter.complete(request, spec.name)
        latency_ms = round((time.perf_counter() - start) * 1000, 2)

        return ChatResponse(
            request_id=request_id,
            model=spec.name,
            provider=spec.provider,
            output=result.output,
            usage=Usage(input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens),
            cost_usd=spec.cost(result.input_tokens, result.output_tokens),
            latency_ms=latency_ms,
            metadata={**result.metadata, "raw_model": result.raw_model,
                      "team_id": request.team_id, "feature": request.feature,
                      "priority": request.priority.value},
        )