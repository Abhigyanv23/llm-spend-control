# LLM Spend Control Center

A routing and budgeting gateway that sits in front of LLM providers (OpenAI, Anthropic, Ollama).
It tracks usage per team and feature, routes requests to cost-effective models, checks whether
the cheap answers were good enough, and warns or blocks usage when budgets are at risk.

> Portfolio project exploring LLMOps: cost optimisation, model routing, budget enforcement,
> quality verification, and the trade-off between cost and answer quality.

## Why

Sending every request to the strongest model is simple but expensive. Most production LLM
traffic (extraction, formatting, short summaries) doesn't need a frontier model. This gateway
makes cost visible, enforceable, and optimisable without every calling service having to care,
and measures whether the cheaper answers are actually good enough.

## Features

| Feature | Status |
|---|---|
| Unified request/response schema across providers | ✅ Phase 1 |
| Model registry (prices, tiers, context limits) in YAML | ✅ Phase 1 |
| Provider adapters: OpenAI, Anthropic, Ollama, Mock | ✅ Phase 1 |
| Normalised error handling (retryable vs non-retryable) | ✅ Phase 1 |
| Request audit log in PostgreSQL (every request, incl. failures and blocks) | ✅ Phase 2 |
| Exact money: `Decimal` / `NUMERIC(14,8)` / integer nano-dollars in Redis | ✅ Phase 2 |
| Per-team and per-feature daily & monthly budgets | ✅ Phase 2 |
| Atomic reserve-then-settle enforcement (Redis Lua) | ✅ Phase 2 |
| Warnings at 80%, blocks at 100%, audited overrides for high priority | ✅ Phase 2 |
| Reconciliation of Redis counters from Postgres | ✅ Phase 2 |
| Usage and budget APIs (`/v1/usage`, `/v1/budgets`) | ✅ Phase 2 |
| Complexity-based model routing (3 tiers, rule-based classifier with confidence and reasons) | ✅ Phase 3 |
| Routing policy in `config/routing.yaml`: profiles, feature rules, validated at startup | ✅ Phase 3 |
| Budget-aware downgrade: cheaper allowed tier before a 402 block | ✅ Phase 3 |
| Savings vs "everything on the strongest model" on every request | ✅ Phase 3 |
| Routing APIs (`/v1/routing`, `/v1/route/preview` dry run) | ✅ Phase 3 |
| Deterministic sampling of cheap answers for async verification (10% base, 50% when unsure) | ✅ Phase 4 |
| Verification queue on Redis Streams + worker process (`python -m app.worker`) | ✅ Phase 4 |
| Judges: similarity (dev) and LLM-as-judge (production), Strategy pattern | ✅ Phase 4 |
| Routing misses stored as labelled data; JSONL export | ✅ Phase 4 |
| Synchronous escalation: pre-call tier bump + post-call cascade (empty, refusal, truncated, bad JSON) | ✅ Phase 4 |
| Verification spend capped by its own budget; net savings after quality overhead | ✅ Phase 4 |
| Quality APIs (`/v1/quality`, `/v1/quality/misses`, `/v1/quality/queue`) | ✅ Phase 4 |
| Cost dashboard | ⏳ Phase 5 |
| 1,000-request simulation & savings report | ⏳ Phase 6 |

## Tech Stack

Python 3.11+ · FastAPI · httpx (async) · Pydantic v2 · PyYAML · PostgreSQL 16 · SQLAlchemy 2.0
(async) + asyncpg · Alembic · Redis 7 (Lua scripts, Streams) · Docker Compose · pytest + fakeredis
*(Coming: Streamlit · scikit-learn)*

## Quickstart

Prerequisites: Python 3.11+, Docker Desktop (running).

```powershell
python -m venv .venv
.venv\Scripts\activate                       # Linux/Mac: source .venv/bin/activate
python -m pip install -r requirements.txt
copy .env.example .env                       # Linux/Mac: cp .env.example .env

docker compose up -d                         # Postgres 16 + Redis 7
docker compose ps                            # wait until both show "(healthy)"

python -m alembic upgrade head               # create tables (migrations 0001 + 0002)
python scripts/seed_budgets.py               # example budget policies (incl. quality-verifier)
python -m uvicorn app.main:app --reload      # terminal 1: the API
python -m app.worker                         # terminal 2: the verification worker
```

Open **http://127.0.0.1:8000/docs** for the interactive API. The worker is optional: without
it, sampled jobs simply wait in the queue until one runs.

### Tests

```powershell
python -m pytest                             # unit + in-process API tests (no Docker needed)
python scripts/smoke_test.py                 # end-to-end: gateway + budgets (running server)
python scripts/smoke_routing.py              # end-to-end: routing (running server, ROUTING_PROFILE=dev)
python scripts/smoke_quality.py              # end-to-end: escalation, queue, worker, quality API
```

`pytest` uses fakeredis and SQLite, so it runs anywhere in seconds. The smoke tests exercise
the real Postgres + Redis. They create their own tiny budgets on fresh team ids, so they can
be re-run any number of times. `smoke_quality.py` runs `python -m app.worker --once` itself.

### Example requests

Linux/Mac (bash):
```bash
curl -X POST http://127.0.0.1:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"team_id":"search","feature":"summarize","messages":[{"role":"user","content":"Hello gateway"}]}'
```

Windows (PowerShell):
```powershell
$body = @{ team_id = "search"; feature = "summarize"
           messages = @(@{ role = "user"; content = "Hello gateway" }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/chat -Method Post -ContentType "application/json" -Body $body

# Set a budget, then check it
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/budgets/team/search -Method Put `
  -ContentType "application/json" -Body '{"daily_limit_usd": "5.00", "monthly_limit_usd": "100.00"}'
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/budgets/team/search/status

# High-priority request that is allowed past an exhausted budget
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/chat -Method Post -ContentType "application/json" `
  -Headers @{ "X-Budget-Override" = "incident INC-42" } `
  -Body '{"team_id":"demo-tiny","feature":"summarize","priority":"high","max_tokens":2000,"messages":[{"role":"user","content":"hi"}]}'
```

## Routing

Requests **without** a `model` field are routed by complexity. Requests **with** one are
honoured as-is.

| Tier | Work | `dev` profile | `production` profile |
|---|---|---|---|
| 1 | Extraction and formatting | `mock-echo` | `gpt-4o-mini`, then `claude-haiku-4-5` |
| 2 | Summarisation and classification | `mock-medium` | `claude-haiku-4-5` |
| 3 | Reasoning-heavy or high-risk | `mock-large` | `claude-sonnet-5-5` |

Decision order: **explicit model** > **pinned feature** > **classifier tier** clamped to the
feature's `[min_tier, max_tier]` (and an optional priority floor). Within a tier, the first
candidate that is available (API key set), fits the context window and has the required
capabilities is chosen. When a budget would block the request, the gateway first tries the
cheaper allowed tiers (unless the feature sets `budget_downgrade: false`).

The policy lives in [`config/routing.yaml`](config/routing.yaml): tier candidates per profile,
feature rules (`min_tier`, `max_tier`, `pin_model`, `requires`, `budget_downgrade`) and
classifier keywords. It is validated against `config/models.yaml` at startup.

See how a prompt would be routed without calling any model:

```powershell
$body = @{ team_id = "demo"; feature = "playground"
           messages = @(@{ role = "user"; content = "Analyze the trade-offs of this design" }) } | ConvertTo-Json -Depth 5
(Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/route/preview -Method Post `
  -ContentType "application/json" -Body $body).routing
```

Every successful `/v1/chat` response (and audit row) carries `metadata.routing`: tier, source,
reasons, classifier features and confidence, any budget downgrades, and the baseline cost and
savings versus the strongest model.

## Quality checks and escalation

Routing is a guess. Phase 4 checks it in two ways, configured in
[`config/quality.yaml`](config/quality.yaml):

**1. Asynchronous verification (measurement).** After a successful routed response on tier 1
or 2, the gateway decides deterministically (a hash of the request id) whether to sample it:
10% normally, 50% when the classifier was unsure. Sampled jobs go to a Redis Stream. The
worker re-asks a stronger **reference model** (first usable tier-3 model), a **judge** compares
the two answers (`similarity` in dev, `llm` in production), and the verdict is stored in
`verifications`. A failed cheap answer is also stored as a **routing miss**: labelled data for
a future learned classifier.

```powershell
python -m app.worker                 # continuous; Ctrl+C once = finish the batch and stop
python -m app.worker --once          # drain what's queued now, print a summary, exit
python scripts/export_misses.py --out misses.jsonl     # routing misses as JSONL
```

Delivery is at-least-once (ack after the result is stored) with idempotency via a UNIQUE
`request_id`; failed jobs are retried after `job_timeout_s` and dead-lettered after
`max_attempts`. Verification calls are real spend, so they are reserved and settled against
their own budget (`quality-verifier` / `verification`): when it is exhausted, verifications are
recorded as `skipped`.

**2. Synchronous escalation (correction), a cascade inside `/v1/chat`.**
- *Pre-call*: a `high`/`critical` request the classifier is unsure about (confidence < 0.6)
  starts one tier higher. Still one call.
- *Post-call*: if the cheap answer is empty, a refusal, cut off (`finish_reason` length), or
  invalid JSON when JSON was asked for, the request is retried once on the next tier up. Each
  attempt is reserved and settled separately; the audit row has the summed cost and every
  attempt in `metadata.escalation`. If the escalation is blocked by the budget, the original
  answer is returned with a note: escalation never fails a request.

In the `dev` profile, mocks fail realistically for free: `mock-echo` only "understands" the
first 200 characters (`mock-medium` 1,000, `mock-large` 4,000), and tier-1 mocks honour test
directives in the prompt: `[[mock:empty]]`, `[[mock:refuse]]`, `[[mock:truncate]]`,
`[[mock:badjson]]`.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/v1/quality"          # miss rate ± margin, escalations, net savings
Invoke-RestMethod "http://127.0.0.1:8000/v1/quality/misses"   # routing misses (prompt preview only)
Invoke-RestMethod "http://127.0.0.1:8000/v1/quality/queue"    # stream length, pending, dead letters
```

**Verification costs money.** In our dev runs, verifying long prompts at a 50% sample rate
cost 20× to 90× the requests being checked, turning net savings negative. Sampling rates are
a budget decision; `/v1/quality` reports `net_savings_usd` after verification spend.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `getaddrinfo failed` during `pip install` | No DNS/internet, or a proxy is required | Check `nslookup pypi.org`; use `--proxy` or another network |
| `ModuleNotFoundError` with a traceback path outside `.venv` | A global `python`/`uvicorn` ran instead of the venv's | Activate the venv (`.venv\Scripts\Activate.ps1`); check `(Get-Command python).Source` |
| `ImportError: cannot import name X` | File exists but doesn't define `X` (unsaved or wrong file) | Check the file's contents; save it |
| `can't open file ...scripts\...` / `No 'script_location' key found` | Running from the wrong folder | `cd` into the project folder (where `alembic.ini` is) |
| `Cannot bind parameter 'Headers'` in PowerShell | `curl` is an alias for `Invoke-WebRequest` | Use `Invoke-RestMethod` (above) or the smoke test scripts |
| Slow installs / "file in use" errors | Project inside a OneDrive-synced folder | Move the project outside OneDrive and recreate `.venv` |
| `docker: command not found` / `error during connect` | Docker Desktop not installed or not running (Windows Home also needs WSL 2) | Start Docker Desktop, wait for "Engine running", open a **new** terminal (restart VS Code) |
| `Bind for 127.0.0.1:5432 failed: port is already allocated` | A local Postgres (or another container) uses 5432 | Set `POSTGRES_PORT=5433` in `.env` and use `:5433` in `DATABASE_URL` |
| `password authentication failed for user "spend"` | Password changed after the volume was created (it's only applied on first init) | `docker compose down -v` (deletes data) then `up -d`, or keep the old password |
| `ConnectionRefusedError` / `[WinError 1225]` from asyncpg or Alembic | Postgres container not running/healthy yet | `docker compose ps`; wait for `(healthy)`; `docker compose logs postgres` |
| `relation "request_logs"` (or `"verifications"`) `does not exist` | Migrations not applied | `python -m alembic upgrade head` |
| `/health` shows `"redis": "down (...)"`, responses carry `X-Budget-Warning: budget-check-unavailable` | Redis unreachable; `BUDGET_FAIL_MODE=open` lets traffic through unchecked | `docker compose up -d redis`; restart the app so it reconciles |
| All requests fail with `503 budget_unavailable` | Redis/Postgres unreachable and `BUDGET_FAIL_MODE=closed` | Bring the dependency back, or switch to `open` for local dev |
| `503 database_unavailable` on `/v1/usage`, `/v1/budgets` or `/v1/quality` | Postgres unreachable | As above for Postgres |
| Budgets look wrong after `docker compose restart redis` or `FLUSHALL` | Redis counters lost or stale | Restart the app, or `POST /v1/budgets/reconcile` (rebuilds from Postgres) |
| Server won't start: `RoutingConfigError: ...` | Typo or invalid rule in `config/routing.yaml` | Fix the line named in the message; the config is validated at startup on purpose |
| Server won't start: `QualityConfigError: No usable reference model ...` | `VERIFY_ENABLED=true` but no tier-3 model is usable (e.g. `production` profile without `ANTHROPIC_API_KEY`) | Set the API key, use `ROUTING_PROFILE=dev`, or `VERIFY_ENABLED=false` |
| `503 no_route` | No available model in the allowed tiers | Set the API keys, use `ROUTING_PROFILE=dev`, or adjust the feature rule |
| A request went to an unexpected model | Classifier keywords / feature rules / pre-call escalation | `POST /v1/route/preview` shows the tier, reasons, keyword hits and `pre_call_escalation` |
| `/v1/quality` shows 0 verifications | No worker ran, or nothing was sampled yet | `python -m app.worker --once`; check `/v1/quality/queue` (`length`, `pending`) |
| `/v1/quality/queue` shows `pending` > 0 that never drains | A worker crashed mid-job | Start a worker: jobs idle longer than `job_timeout_s` are reclaimed automatically |
| `dead_letter` > 0 | Jobs failed `max_attempts` times or were malformed | `docker exec spend-redis redis-cli XRANGE quality:verify:dead - +` shows the reasons |
| Verifications are all `skipped` | The `quality-verifier` budget is exhausted | Raise it (`PUT /v1/budgets/team/quality-verifier`) or lower the sample rates |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | Enables OpenAI models |
| `ANTHROPIC_API_KEY` | — | Enables Anthropic models |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server |
| `MODEL_REGISTRY_PATH` | `config/models.yaml` | Model catalogue |
| `REQUEST_TIMEOUT_S` | `60` | Upstream call timeout |
| `DATABASE_URL` | `postgresql+asyncpg://spend:spend_dev_password@127.0.0.1:5432/spend` | Audit log, policies, verifications (source of truth) |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Budget counters and the verification stream |
| `REDIS_TIMEOUT_S` | `0.5` | Redis socket timeout for the API; keeps "Redis is down" decisions fast (the worker uses ≥ 5 s) |
| `BUDGET_WARN_THRESHOLD` | `0.8` | Fraction of a limit that triggers warnings and alerts |
| `BUDGET_FAIL_MODE` | `open` | `open`: allow + warn if Redis is down; `closed`: reject with 503 |
| `RECONCILE_ON_STARTUP` | `true` | Rebuild Redis counters from Postgres at startup (request and verification spend) |
| `ROUTING_CONFIG_PATH` | `config/routing.yaml` | Routing policy (tiers, feature rules, classifier keywords) |
| `ROUTING_PROFILE` | `dev` | `dev`: mock model per tier (priced like real ones); `production`: real providers |
| `QUALITY_CONFIG_PATH` | `config/quality.yaml` | Sampling, verification, escalation and privacy policy |
| `VERIFY_ENABLED` | `true` | `false` disables sampling and async verification (escalation still works) |
| `WORKER_CONCURRENCY` | `4` | Verification jobs processed in parallel per worker |
| `WORKER_CONSUMER_NAME` | `<hostname>-<pid>` | Consumer name in the Redis consumer group; unique per worker process |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `spend` / `spend_dev_password` / `spend` | Used by `docker compose` to initialise Postgres |
| `POSTGRES_PORT` / `REDIS_PORT` | `5432` / `6379` | Host ports published by `docker compose` |

Model names and prices in `config/models.yaml` are illustrative. Verify them against each
provider's pricing page before relying on the cost numbers.

## Project layout

```
app/
├── main.py            App factory, lifespan, error handlers, /health, /v1/chat
├── bootstrap.py       Composition root shared by the API and the worker
├── worker.py          python -m app.worker (verification worker CLI)
├── gateway.py         Pipeline: route -> pre-call escalation -> reserve (downgrade) -> call
│                      -> settle -> post-call checks / cascade -> sample for verification
├── audit.py           Audit-log writer (background) + usage queries
├── money.py           Decimal / nano-dollar helpers
├── tokens.py          Pre-call token estimation
├── registry.py        Model registry + exact Decimal cost calculation
├── schemas.py         Canonical request/response + budget/usage models
├── errors.py          Normalised error types (incl. 402 / 503 budget and routing errors)
├── config.py          Environment settings
├── api/               /v1/usage, /v1/budgets, /v1/routing, /v1/quality routers
├── budgets/           periods, Lua scripts, Redis store, policies, service, reconciliation
├── routing/           routing config loader, rule-based classifier, router
├── quality/           quality config, sampling, judges, escalation, jobs, queue, worker, reports
├── db/                SQLAlchemy engine/session + ORM models
└── providers/         One adapter per provider (Adapter pattern); mock with capability limits
migrations/            Alembic environment + versioned schema migrations (0001, 0002)
config/                models.yaml, routing.yaml, quality.yaml
scripts/               Smoke tests, budget seeding, routing-miss export
tests/                 pytest suite (fakeredis + SQLite)
docs/                  Architecture, API reference, per-phase design, theory notes, change records
docker-compose.yml     Postgres 16 + Redis 7
```

## Known limitations

- No authentication: anyone who can reach the API can change budgets, read usage, or read the prompts stored in routing misses.
- Token estimation before the call is a ~4 chars/token heuristic; real input can exceed it.
- Reconciliation resets in-flight holds, so it is only safe with a single gateway instance or during a quiet window.
- The rule-based classifier is English-only and keyword-driven (no stemming beyond plurals, blind to negation).
- Savings are a counterfactual estimate: the strongest model might have produced a different number of output tokens.
- The similarity judge only suits the mocks; real answers need the LLM judge, which has its own biases.
- Post-call checks only catch *visible* failures; a fluent but wrong answer is only found by sampling.
- Routing and quality config are loaded at startup; changes need a restart.
- Full lists: [Phase 2](docs/phases/phase-2-budgets.md#known-limitations) · [Phase 3](docs/phases/phase-3-routing.md#known-limitations) · [Phase 4](docs/phases/phase-4-quality.md#known-limitations)

## Documentation

- [Architecture](docs/architecture.md)
- [API Reference](docs/api.md)
- [Changelog](CHANGELOG.md)
- Phase notes: [Phase 1: Gateway](docs/phases/phase-1-gateway.md) · [Phase 2: Budgets](docs/phases/phase-2-budgets.md) · [Phase 3: Routing](docs/phases/phase-3-routing.md) · [Phase 4: Quality](docs/phases/phase-4-quality.md)
- Theory notes: [Phase 2](docs/notes/phase-2-theory.md) · [Phase 3](docs/notes/phase-3-theory.md) · [Phase 4](docs/notes/phase-4-theory.md)
- Change records: [Phase 2](docs/phases/phase-2-changes.md) · [Phase 3](docs/phases/phase-3-changes.md) · [Phase 4](docs/phases/phase-4-changes.md)

## Authors

Abhigyan Varma

Enrique Dias
