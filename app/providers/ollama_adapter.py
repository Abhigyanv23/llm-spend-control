import httpx

from app.providers.base import ProviderAdapter, ProviderResult, post_json
from app.schemas import ChatRequest


class OllamaAdapter(ProviderAdapter):
    name = "ollama"

    def __init__(self, client: httpx.AsyncClient, base_url: str):
        self.client = client
        self.url = f"{base_url.rstrip('/')}/api/chat"

    async def complete(self, request: ChatRequest, model: str) -> ProviderResult:
        payload = {
            "model": model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "options": {"num_predict": request.max_tokens,
                        "temperature": request.temperature},
        }
        data = await post_json(self.client, self.name, self.url, payload)

        return ProviderResult(
            output=(data.get("message") or {}).get("content", ""),
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            raw_model=data.get("model", model),
            metadata={"finish_reason": data.get("done_reason")},
        )