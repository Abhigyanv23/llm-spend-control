import httpx

from app.errors import ProviderError
from app.providers.base import ProviderAdapter, ProviderResult, post_json
from app.schemas import ChatRequest


class OpenAIAdapter(ProviderAdapter):
    name = "openai"
    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, client: httpx.AsyncClient, api_key: str | None):
        self.client = client
        self.api_key = api_key

    async def complete(self, request: ChatRequest, model: str) -> ProviderResult:
        if not self.api_key:
            raise ProviderError(self.name, "OPENAI_API_KEY is not set", status_code=503)

        payload = {
            "model": model,
            "messages": [m.model_dump() for m in request.messages],
            # Note: some newer OpenAI models expect "max_completion_tokens" instead
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        data = await post_json(self.client, self.name, self.URL, payload,
                               headers={"Authorization": f"Bearer {self.api_key}"})

        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return ProviderResult(
            output=choice["message"].get("content") or "",
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            raw_model=data.get("model", model),
            metadata={"finish_reason": choice.get("finish_reason"),
                      "provider_request_id": data.get("id")},
        )