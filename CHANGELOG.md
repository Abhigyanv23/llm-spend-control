# Changelog

All notable changes, grouped by build phase.

## [0.3.0] - Phase 3: Request Complexity Routing

### Added
- Tiered model routing: tier 1 extraction/formatting, tier 2 summarisation/classification,
  tier 3 reasoning-heavy or high-risk work.
- `config/routing.yaml`: per-profile tier candidates (`dev` mocks, `production` real models),
  feature rules (`min_tier`, `max_tier`, `pin_model`, `requires`, `budget_downgrade`),
  optional `priority_min_tier`, and classifier keywords. Validated at startup.
- Rule-based complexity classifier (`rules-v1`) with confidence scores and reasons.
- Router with a fixed decision order: explicit > pinned > classifier within bounds.
- Budget-aware downgrade: a budget-blocked request falls back to a cheaper allowed tier.
- Counterfactual baseline cost and savings on every successful request.
- `GET /v1/routing` (active policy + available providers).
- `POST /v1/route/preview` (dry-run routing decision; no call, reservation or audit row).
- `503 no_route` error when no available model satisfies the constraints.
- Mock models `mock-medium` (tier 2) and `mock-large` (tier 3), priced like real tiers.
- New settings `ROUTING_CONFIG_PATH`, `ROUTING_PROFILE`.
- `tests/test_classifier.py`, `tests/test_router.py`, `tests/test_routing_api.py` (33 tests).
- `scripts/smoke_routing.py`: live routing checks against Postgres + Redis.

### Changed
- Requests without `model` are now routed instead of using `defaults.model`.
- `metadata.routing` added to `/v1/chat` responses and audit rows.
- `/health` now includes `routing_profile`.

## [0.2.0] - Phase 2: Cost Tracking and Budgets

### Added
- PostgreSQL audit trail (`request_logs`): one row per `/v1/chat` request, including provider
  errors, validation errors and budget blocks (cost 0 when no provider was reached).
- Budget policies (`budget_policies`) per team and per feature, with daily and monthly limits
  (UTC calendar day / month; `NULL` = unlimited).
- Real-time enforcement with **reserve-then-settle**: the worst-case cost is held atomically in
  Redis (Lua script) before the provider call and swapped for the actual cost afterwards;
  `try/finally` guarantees a hold is always released.
- Warnings at `BUDGET_WARN_THRESHOLD` (default 80%): `X-Budget-Warning` header,
  `metadata.budget_warnings`, and a deduplicated row in `budget_alerts`.
- Blocking at 100%: `402 budget_exceeded` (low/normal priority) or `402 override_required`
  (high/critical); high/critical requests may proceed with an `X-Budget-Override` reason,
  which is stored in the audit log.
- `BUDGET_FAIL_MODE` (`open` | `closed`) for when Redis or Postgres is unavailable
  (`503 budget_unavailable` in closed mode).
- Reconciliation that rebuilds Redis counters from `request_logs`, run at startup and via
  `POST /v1/budgets/reconcile`.
- Endpoints: `GET /v1/usage` (filters, totals, pagination), `GET /v1/budgets`,
  `PUT /v1/budgets/{scope}/{scope_id}`, `GET /v1/budgets/{scope}/{scope_id}/status`.
- `/health` now reports Postgres and Redis status (`ok` / `degraded`).
- `docker-compose.yml` with PostgreSQL 16 and Redis 7 (named volumes, healthchecks).
- Alembic migrations (async env); `scripts/seed_budgets.py` for example policies.
- pytest suite (60 tests: money, estimation, periods, Lua atomicity incl. a concurrency race,
  budget service, full HTTP pipeline) using fakeredis and SQLite.
- Smoke test extended with 8 Phase 2 checks; `GATEWAY_URL` env var to target another server.

### Changed
- **Breaking:** `cost_usd` (and every money field) is now a fixed-point JSON **string**
  (e.g. `"0.00000430"`) backed by `Decimal`, not a float. Model prices in `/v1/models` are
  strings too.
- **Breaking:** `team_id` and `feature` must match `^[A-Za-z0-9._-]+$` (max 64 chars), since
  they become Redis keys and header values.
- Cost calculation uses `Decimal` end to end (`ModelSpec.cost` returns `Decimal`).
- Token estimation moved to `app/tokens.py` and adds a 4-token per-message overhead; the
  context check uses the same estimate.
- `app.main` is now an app factory (`create_app`) so dependencies can be injected in tests.
- Database errors on read endpoints return a structured `503 database_unavailable`.

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
