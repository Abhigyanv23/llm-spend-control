import asyncio
import random

from app.providers.base import ProviderAdapter, ProviderResult
from app.schemas import ChatRequest


def estimate_tokens(text: str) -> int:
    """Rough heuristic: ~4 characters per token for English text."""
    return max(1, len(text) // 4)


class MockAdapter(ProviderAdapter):
    """Fake provider: lets you test the whole pipeline with no keys and no cost.
    We'll also use it for the 1,000-request simulation in Phase 6."""
    name = "mock"

    async def complete(self, request: ChatRequest, model: str) -> ProviderResult:
        await asyncio.sleep(random.uniform(0.03, 0.08))   # simulate network latency
        prompt = " ".join(m.content for m in request.messages)
        last_user = next((m.content for m in reversed(request.messages)
                          if m.role == "user"), "")
        output = f"[mock:{model}] You said: {last_user[:200]}"
        output_tokens = min(estimate_tokens(output), request.max_tokens)
        return ProviderResult(
            output=output,
            input_tokens=estimate_tokens(prompt),
            output_tokens=output_tokens,
            raw_model=model,
            metadata={"finish_reason": "stop"},
        )