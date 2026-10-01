from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

from app.errors import ProviderError
from app.schemas import ChatRequest

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}


@dataclass
class ProviderResult:
    """What every adapter must return. Cost/latency are computed centrally."""
    output: str
    input_tokens: int
    output_tokens: int
    raw_model: str
    metadata: dict = field(default_factory=dict)


class ProviderAdapter(ABC):
    name: str = "base"

    @abstractmethod
    async def complete(self, request: ChatRequest, model: str) -> ProviderResult:
        ...


async def post_json(client: httpx.AsyncClient, provider: str, url: str,
                    payload: dict, headers: dict | None = None) -> dict:
    """Shared HTTP call with normalised error handling for all adapters."""
    try:
        resp = await client.post(url, json=payload, headers=headers or {})
    except httpx.TimeoutException as e:
        raise ProviderError(provider, "Upstream timeout", status_code=504, retryable=True) from e
    except httpx.HTTPError as e:
        raise ProviderError(provider, f"Network error: {e}", status_code=502, retryable=True) from e

    if resp.status_code >= 400:
        retryable = resp.status_code in RETRYABLE_STATUS
        # Upstream 429 is passed through; any other upstream failure is a 502 (bad gateway)
        status = 429 if resp.status_code == 429 else 502
        raise ProviderError(provider, f"Upstream HTTP {resp.status_code}: {resp.text[:300]}",
                            status_code=status, retryable=retryable)
    return resp.json()
