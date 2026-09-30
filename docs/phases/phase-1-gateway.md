# Phase 1: Unified Request Gateway

## Goal
Put one service in front of all LLM providers so later phases (budgets, routing, verification)
have a single place to live.

## What was built
- Canonical `ChatRequest` / `ChatResponse` schemas
- YAML model registry with cost calculation
- Adapters for OpenAI, Anthropic, Ollama, Mock
- Normalised error handling
- FastAPI app with a shared async HTTP client

## Design decisions

**Gateway pattern.** A single choke point gives full visibility and control over LLM spend.
Without it, cost tracking and budgets would have to be reimplemented in every caller.

**Canonical schema.** Providers differ in system-prompt placement and token field names
(`prompt_tokens` vs `input_tokens` vs `prompt_eval_count`). Translating at the edges keeps the
core logic provider-agnostic.

**Adapters return tokens, not cost.** Cost depends on registry prices. Computing it centrally
means a price change is a one-line YAML edit, not four code changes.

**Registry in YAML.** Prices and model lineups change often, so they are data, not code.
The registry is also the foundation for tier-based routing in Phase 3.

**Async I/O + shared client.** LLM calls are I/O-bound (mostly waiting). `async` lets one
worker serve many concurrent requests, and a shared `httpx.AsyncClient` reuses TCP/TLS
connections through pooling.

**`perf_counter` for latency.** It is monotonic and high-resolution, unaffected by system clock changes.

**Retryable flag on errors.** 429 / 5xx / timeouts are transient; 400 / 401 are not. This
distinction will drive fallback and escalation later.

**Mock provider.** Enables testing, CI and large simulations at zero cost.

## Known limitations (addressed later)
- Costs use floats; switching to exact `NUMERIC` storage in Phase 2.
- Token estimate for the context check is a rough heuristic (~4 chars/token).
- No persistence: requests are not yet logged (Phase 2).
- No retries or fallback between providers yet.
- Model choice is manual/default; no routing yet (Phase 3).

## Key formula
cost = (input_tokens × input_price_per_MTok + output_tokens × output_price_per_MTok) / 1,000,000