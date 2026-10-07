# Changelog

All notable changes, grouped by build phase.

## [0.5.0] - Phase 5: Cost Dashboard

### Added
- Migration `0003`: analytics columns on `request_logs` (`routed_tier`, `route_source`,
  `classifier_confidence`, `baseline_cost_usd`, `escalated`, `pre_escalated`, `downgraded`,
  `prompt_fingerprint`, `prompt_preview`), backfilled from `metadata` (set-based on Postgres),
  new indexes `(model, created_at)` and `(prompt_fingerprint, created_at)`, and the team index
  upgraded to covering (`INCLUDE (cost_usd)`).
- Prompt fingerprints (`app/fingerprint.py`): SHA-256 of normalised instruction text; optional
  preview controlled by `privacy.prompt_preview_chars` (and `store_prompts`).
- Analytics layer `app/analytics/`: zero-filled daily spend by team/feature/model, cost by model,
  top requests and prompt patterns, month-end projections (run-rate, trailing 7-day, EWMA) with
  status, exhaustion date and burn-down, gross/net savings by feature and tier, routing quality
  with Wilson 95% intervals, p50/p95/p99 latency (Postgres `percentile_cont` + Python fallback),
  error breakdown, headline KPIs.
- `/v1/analytics/summary`, `/spend`, `/projections`, `/top`, `/savings`, `/quality`,
  `/latency`, `/errors` with window validation (`400 invalid_window`) and a TTL cache.
- Streamlit dashboard `dashboard/app.py` (6 tabs, Altair charts, reads only from the API).
- `scripts/seed_demo_data.py` (deterministic, idempotent, `--reset`) and
  `scripts/smoke_dashboard.py` (22 checks including a headless dashboard render).
- Settings `ANALYTICS_CACHE_TTL_S`, `ANALYTICS_MAX_WINDOW_DAYS`; `GATEWAY_URL` for the dashboard.
- 44 new tests (254 total).

### Changed
- The gateway writes the analytics columns and a prompt fingerprint for every request.
- `requirements.txt` gains a dashboard section (`streamlit`, `altair`).
- App version `0.5.0`.

## [0.4.0] - Phase 4: Quality Checks and Escalation

### Added
- `config/quality.yaml`: sampling, verification, escalation and privacy policy, validated at
  startup against the model registry and routing profile (fail fast).
- Deterministic hash-based sampling of successful routed responses on tiers 1–2 (10% base
  rate, 50% when classifier confidence < 0.6); decision recorded in `metadata.quality.sampling`.
- Verification queue on **Redis Streams**: consumer group, `XACK` after the result is stored,
  `XAUTOCLAIM` of stale jobs, retries up to `max_attempts`, dead-letter stream, approximate
  `MAXLEN`. Enqueueing runs in a background task and can never fail a request.
- Worker process `python -m app.worker` (`--once`, `--concurrency`, `--consumer`) with bounded
  concurrency, graceful Ctrl+C shutdown on Windows, and consumer cleanup.
- Judges behind one interface: `SimilarityJudge` (difflib/Jaccard, deterministic) and
  `LLMJudge` (strict-JSON grading against a reference answer; unparseable → `inconclusive`).
- Verification budget: reference + judge calls are reserved/settled under
  `quality-verifier` / `verification`; when blocked, the verification is stored as `skipped`.
- Migration `0002`: `verifications` (UNIQUE `request_id` for idempotency) and
  `routing_misses` (labelled examples; prompt stored only if `privacy.store_prompts`, capped).
- Synchronous escalation: pre-call tier bump for uncertain high/critical requests; post-call
  checks (empty, refusal, truncated, invalid JSON) with a one-step cascade to the next tier;
  budget-blocked or failed escalations return the original answer with a note.
- `metadata.escalation` on responses and audit rows (every attempt, its cost and check).
- `GET /v1/quality` (miss rate ± 95% margin, weighted miss rate, misses by model/feature,
  escalation counts, verification spend, net savings), `GET /v1/quality/misses`,
  `GET /v1/quality/queue`.
- `scripts/export_misses.py`: routing misses as JSONL training data.
- `scripts/smoke_quality.py`: 19 live checks against Postgres + Redis.
- Mock capability limits (`mock-echo` 200 chars, `mock-medium` 1,000, `mock-large` 4,000) and
  tier-1 test directives `[[mock:empty|refuse|truncate|badjson]]`.
- `app/bootstrap.py`: one composition root for the API and the worker.
- Settings `QUALITY_CONFIG_PATH`, `VERIFY_ENABLED`, `WORKER_CONCURRENCY`, `WORKER_CONSUMER_NAME`.
- Seeded `quality-verifier` team budget ($1.00/day, $20.00/month).
- 117 new tests (210 total).

### Changed
- Routed `high`/`critical` requests with low classifier confidence start one tier higher.
- `cost_usd`, `usage` and the audit row of an escalated request are the **sum of all attempts**;
  `model` is the model that produced the returned answer.
- `/v1/route/preview` applies pre-call escalation and returns `pre_call_escalation`.
- Startup reconciliation also restores verification spend into the verifier's counters.
- Starting with `VERIFY_ENABLED=true` and no usable tier-3 model now fails at startup with
  `QualityConfigError` (e.g. `production` profile without `ANTHROPIC_API_KEY`).
- `.gitignore` ignores `*.jsonl` (exports contain prompts).

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
