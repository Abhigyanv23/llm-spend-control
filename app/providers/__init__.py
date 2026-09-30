import httpx

from app.config import Settings
from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.base import ProviderAdapter
from app.providers.mock_adapter import MockAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter


def build_adapters(client: httpx.AsyncClient, settings: Settings) -> dict[str, ProviderAdapter]:
    """Factory: provider name (as used in models.yaml) -> adapter instance."""
    return {
        "mock": MockAdapter(),
        "openai": OpenAIAdapter(client, settings.openai_api_key),
        "anthropic": AnthropicAdapter(client, settings.anthropic_api_key),
        "ollama": OllamaAdapter(client, settings.ollama_base_url),
    }