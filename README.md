# LLM Spend Control Center

A routing and budgeting gateway that sits in front of LLM providers (OpenAI, Anthropic, Ollama).
It tracks usage per team and feature, routes requests to cost-effective models, and warns or
blocks usage when budgets are at risk.

> Portfolio project exploring LLMOps: cost optimisation, model routing, budget enforcement,
> and the trade-off between cost and answer quality.

## Why

Sending every request to the strongest model is simple but expensive. Most production LLM
traffic (extraction, formatting, short summaries) doesn't need a frontier model. This gateway
makes cost visible, enforceable, and optimisable without every calling service having to care.

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
| Async quality verification & escalation | ⏳ Phase 4 |
| Cost dashboard | ⏳ Phase 5 |
| 1,000-request simulation & savings report | ⏳ Phase 6 |

## Tech Stack

Python 3.11+ · FastAPI · httpx (async) · Pydantic v2 · PyYAML · PostgreSQL 16 · SQLAlchemy 2.0
(async) + asyncpg · Alembic · Redis 7 (Lua scripts) · Docker Compose · pytest + fakeredis
*(Coming: scikit-learn · Celery/RQ · Streamlit)*

## Quickstart

Prerequisites: Python 3.11+, Docker Desktop (running).

```powershell
python -m venv .venv
.venv\Scripts\activate                       # Linux/Mac: source .venv/bin/activate
python -m pip install -r requirements.txt
copy .env.example .env                       # Linux/Mac: cp .env.example .env

docker compose up -d                         # Postgres 16 + Redis 7
docker compose ps                            # wait until both show "(healthy)"

python -m alembic upgrade head               # create tables
python scripts/seed_budgets.py               # example budget policies
python -m uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/docs** for the interactive API.

### Tests

```powershell
python -m pytest                             # unit + in-process API tests (no Docker needed)
python scripts/smoke_test.py                 # end-to-end: gateway + budgets (running server)
python scripts/smoke_routing.py              # end-to-end: routing (running server, ROUTING_PROFILE=dev)
```

`pytest` uses fakeredis and SQLite, so it runs anywhere in a few seconds. The smoke tests
exercise the real Postgres + Redis: audit logging, the 80% warning, 402 blocks, the override
flow, the status endpoint, routing decisions, budget-aware downgrade and routing metadata in
the audit log. They create their own tiny budgets on fresh team ids, so they can be re-run
any number of times.

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

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `getaddrinfo failed` during `pip install` | No DNS/internet, or a proxy is required | Check `nslookup pypi.org`; use `--proxy` or another network |
| `ModuleNotFoundError` with a traceback path outside `.venv` | A global `uvicorn` ran instead of the venv's | Always run `python -m uvicorn ...` with the venv activated |
| `ImportError: cannot import name X` | File exists but doesn't define `X` (unsaved or wrong file) | Check the file's contents; save it |
| `Cannot bind parameter 'Headers'` in PowerShell | `curl` is an alias for `Invoke-WebRequest` | Use `Invoke-RestMethod` (above) or the smoke test scripts |
| Slow installs / "file in use" errors | Project inside a OneDrive-synced folder | Move the project outside OneDrive and recreate `.venv` |
| `docker: command not found` / `error during connect` | Docker Desktop not installed or not running | Start Docker Desktop, wait for "Engine running", open a new terminal |
| `Bind for 127.0.0.1:5432 failed: port is already allocated` | A local Postgres (or another container) uses 5432 | Set `POSTGRES_PORT=5433` in `.env` and use `:5433` in `DATABASE_URL` |
| `password authentication failed for user "spend"` | Password changed after the volume was created (it's only applied on first init) | `docker compose down -v` (deletes data) then `up -d`, or keep the old password |
| `ConnectionRefusedError` / `[WinError 1225]` from asyncpg or Alembic | Postgres container not running/healthy yet | `docker compose ps`; wait for `(healthy)`; `docker compose logs postgres` |
| `relation "request_logs" does not exist` | Migrations not applied | `python -m alembic upgrade head` |
| `/health` shows `"redis": "down (...)"`, responses carry `X-Budget-Warning: budget-check-unavailable` | Redis unreachable; `BUDGET_FAIL_MODE=open` lets traffic through unchecked | `docker compose up -d redis`; restart the app so it reconciles |
| All requests fail with `503 budget_unavailable` | Redis/Postgres unreachable and `BUDGET_FAIL_MODE=closed` | Bring the dependency back, or switch to `open` for local dev |
| `503 database_unavailable` on `/v1/usage` or `/v1/budgets` | Postgres unreachable | As above for Postgres |
| Budgets look wrong after `docker compose restart redis` or `FLUSHALL` | Redis counters lost or stale | Restart the app, or `POST /v1/budgets/reconcile` (rebuilds from Postgres) |
| Server won't start: `RoutingConfigError: ...` | Typo or invalid rule in `config/routing.yaml` (unknown model, missing tier, `min_tier > max_tier`) | Fix the line named in the message; the config is validated at startup on purpose |
| `503 no_route` | No available model in the allowed tiers (e.g. `production` profile without API keys, or a `requires` no candidate supports) | Set the API keys, use `ROUTING_PROFILE=dev`, or adjust the feature rule |
| A request went to an unexpected model | Classifier keywords / feature rules | `POST /v1/route/preview` shows the tier, reasons and keyword hits; tune `routing.yaml` |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | Enables OpenAI models |
| `ANTHROPIC_API_KEY` | — | Enables Anthropic models |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server |
| `MODEL_REGISTRY_PATH` | `config/models.yaml` | Model catalogue |
| `REQUEST_TIMEOUT_S` | `60` | Upstream call timeout |
| `DATABASE_URL` | `postgresql+asyncpg://spend:spend_dev_password@127.0.0.1:5432/spend` | Audit log + policies (source of truth) |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Real-time budget counters |
| `REDIS_TIMEOUT_S` | `0.5` | Redis socket timeout; keeps "Redis is down" decisions fast |
| `BUDGET_WARN_THRESHOLD` | `0.8` | Fraction of a limit that triggers warnings and alerts |
| `BUDGET_FAIL_MODE` | `open` | `open`: allow + warn if Redis is down; `closed`: reject with 503 |
| `RECONCILE_ON_STARTUP` | `true` | Rebuild Redis counters from `request_logs` at startup |
| `ROUTING_CONFIG_PATH` | `config/routing.yaml` | Routing policy (tiers, feature rules, classifier keywords) |
| `ROUTING_PROFILE` | `dev` | `dev`: mock model per tier (priced like real ones); `production`: real providers |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `spend` / `spend_dev_password` / `spend` | Used by `docker compose` to initialise Postgres |
| `POSTGRES_PORT` / `REDIS_PORT` | `5432` / `6379` | Host ports published by `docker compose` |

Model names and prices in `config/models.yaml` are illustrative. Verify them against each
provider's pricing page before relying on the cost numbers.

## Project layout

```
app/
├── main.py            App factory, lifespan (wiring), error handlers, /health, /v1/chat
├── gateway.py         Request pipeline: route -> estimate -> reserve (downgrade) -> call -> settle
├── audit.py           Audit-log writer (background) + usage queries
├── money.py           Decimal / nano-dollar helpers
├── tokens.py          Pre-call token estimation
├── registry.py        Model registry + exact Decimal cost calculation
├── schemas.py         Canonical request/response + budget/usage models
├── errors.py          Normalised error types (incl. 402 / 503 budget and routing errors)
├── config.py          Environment settings
├── api/               /v1/usage, /v1/budgets and /v1/routing routers
├── budgets/           periods, Lua scripts, Redis store, policies, service, reconciliation
├── routing/           routing config loader, rule-based classifier, router
├── db/                SQLAlchemy engine/session + ORM models
└── providers/         One adapter per provider (Adapter pattern)
migrations/            Alembic environment + versioned schema migrations
config/models.yaml     Model catalogue (incl. mock models per tier)
config/routing.yaml    Routing policy
scripts/               Smoke tests, budget seeding
tests/                 pytest suite (fakeredis + SQLite)
docs/                  Architecture, API reference, per-phase design and theory notes
docker-compose.yml     Postgres 16 + Redis 7
```

## Known limitations

- No authentication: anyone who can reach the API can change budgets or read usage (planned).
- Token estimation before the call is a ~4 chars/token heuristic; real input can exceed it.
- Reconciliation resets in-flight holds, so it is only safe with a single gateway instance or during a quiet window.
- The rule-based classifier is English-only and keyword-driven (no stemming beyond plurals, blind to negation).
- Savings are a counterfactual estimate: the strongest model might have produced a different number of output tokens.
- Routing config is loaded at startup; changes need a restart.
- Full lists: [Phase 2 notes](docs/phases/phase-2-budgets.md#known-limitations) · [Phase 3 notes](docs/phases/phase-3-routing.md#known-limitations)

## Documentation

- [Architecture](docs/architecture.md)
- [API Reference](docs/api.md)
- [Changelog](CHANGELOG.md)
- Phase notes: [Phase 1: Gateway](docs/phases/phase-1-gateway.md) · [Phase 2: Budgets](docs/phases/phase-2-budgets.md) · [Phase 3: Routing](docs/phases/phase-3-routing.md)
- Theory notes: [Phase 2](docs/notes/phase-2-theory.md) · [Phase 3](docs/notes/phase-3-theory.md)
- Change records: [Phase 2](docs/phases/phase-2-changes.md) · [Phase 3](docs/phases/phase-3-changes.md)

## Authors

Abhigyan Varma

Enrique Dias