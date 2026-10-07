import httpx

from app.errors import ProviderError
from app.providers.base import ProviderAdapter, ProviderResult, post_json
from app.schemas import ChatRequest


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, client: httpx.AsyncClient, api_key: str | None):
        self.client = client
        self.api_key = api_key

    async def complete(self, request: ChatRequest, model: str) -> ProviderResult:
        if not self.api_key:
            raise ProviderError(self.name, "ANTHROPIC_API_KEY is not set", status_code=503)

        # Anthropic takes the system prompt as a separate field, not a message
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        messages = [{"role": m.role, "content": m.content}
                    for m in request.messages if m.role != "system"]
        if not messages:
            raise ProviderError(self.name, "At least one user message is required",
                                status_code=400)

        payload = {
            "model": model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if system:
            payload["system"] = system

        data = await post_json(self.client, self.name, self.URL, payload, headers={
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
        })

        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage") or {}
        return ProviderResult(
            output=text,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            raw_model=data.get("model", model),
            metadata={"finish_reason": data.get("stop_reason"),
                      "provider_request_id": data.get("id")},
        )
