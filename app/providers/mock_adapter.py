import asyncio
import random
import re

from app.providers.base import ProviderAdapter, ProviderResult
from app.schemas import ChatRequest
from app.tokens import estimate_tokens

# How much of the user's message each mock "understands" and echoes back. Stronger mocks
# handle longer inputs, so long prompts give truncated cheap answers that the verifier can
# catch: realistic routing misses at zero cost. Kept here, not in models.yaml, because
# ModelSpec(**entry) rejects unknown keys. Short prompts (<= 200 chars) give identical
# output on every mock, exactly as before Phase 4.
MOCK_CAPABILITY_CHARS = {"mock-echo": 200, "mock-medium": 1000, "mock-large": 4000}
DEFAULT_CAPABILITY_CHARS = 200

# Test directives, honoured only by tier-1 mocks, embedded anywhere in the last user message.
# They make the cheap answer fail on purpose so escalation can be tested deterministically.
DIRECTIVE_MODELS = frozenset({"mock-echo"})
DIRECTIVE = re.compile(r"\[\[mock:(empty|refuse|truncate|badjson)\]\]", re.IGNORECASE)
REFUSAL_TEXT = "I'm sorry, but I can't help with that request."


def find_directive(model: str, text: str) -> str | None:
    if model not in DIRECTIVE_MODELS:
        return None
    match = DIRECTIVE.search(text)
    return match.group(1).lower() if match else None


class MockAdapter(ProviderAdapter):
    """Fake provider: lets you test the whole pipeline with no keys and no cost.
    We'll also use it for the 1,000-request simulation in Phase 6."""
    name = "mock"

    async def complete(self, request: ChatRequest, model: str) -> ProviderResult:
        await asyncio.sleep(random.uniform(0.03, 0.08))   # simulate network latency
        prompt = " ".join(m.content for m in request.messages)
        last_user = next((m.content for m in reversed(request.messages)
                          if m.role == "user"), "")
        capability = MOCK_CAPABILITY_CHARS.get(model, DEFAULT_CAPABILITY_CHARS)
        output = f"[mock:{model}] You said: {last_user[:capability]}"
        output_tokens = min(estimate_tokens(output), request.max_tokens)
        metadata = {"finish_reason": "stop"}

        directive = find_directive(model, last_user)
        if directive == "empty":
            output, output_tokens = "", 0
        elif directive == "refuse":
            output = REFUSAL_TEXT
            output_tokens = min(estimate_tokens(output), request.max_tokens)
        elif directive == "truncate":
            # Hit the output limit: half an answer, every allowed token billed
            output = output[: max(1, len(output) // 2)]
            output_tokens = request.max_tokens
            metadata["finish_reason"] = "length"
        elif directive == "badjson":
            output = '{"answer": "' + last_user[:40].replace('"', "'")    # never closed
            output_tokens = min(estimate_tokens(output), request.max_tokens)
        if directive:
            metadata["mock_directive"] = directive

        return ProviderResult(
            output=output,
            input_tokens=estimate_tokens(prompt),
            output_tokens=output_tokens,
            raw_model=model,
            metadata=metadata,
        )
