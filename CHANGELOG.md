# Changelog

All notable changes, grouped by build phase.

## [0.1.0] - Phase 1: Unified Request Gateway

### Added
- `POST /v1/chat`: single endpoint accepting a canonical chat request (team, feature, priority, optional model).
- `GET /v1/models`: lists registered models with pricing, tier and capabilities.
- `GET /health`: liveness check.
- Model registry loaded from `config/models.yaml`, with per-model cost calculation.
- Provider adapters for OpenAI, Anthropic, Ollama and a Mock provider.
- Shared `httpx.AsyncClient` with connection pooling, managed by FastAPI lifespan.
- Normalised error format with `code`, `message`, `retryable`, and provider info.
- Pre-call context-length check using a token estimate.
- `scripts/smoke_test.py`: end-to-end check of the happy path and all Phase 1 error cases.