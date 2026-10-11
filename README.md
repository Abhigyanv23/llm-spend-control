# LLM Spend Control Center

[![CI](https://github.com/Abhigyanv23/llm-spend-control/actions/workflows/ci.yml/badge.svg)](https://github.com/Abhigyanv23/llm-spend-control/actions/workflows/ci.yml)

A gateway in front of LLM providers (OpenAI, Anthropic, Ollama) that routes every request to the
cheapest model tier that can handle it, enforces per-team and per-feature budgets *before* money is
spent, checks a sample of cheap answers against a stronger model, and shows it all on a cost
dashboard.

> **Reduced simulated LLM spend by 40.3% (net of verification; 63.7% gross) while maintaining a
> 97.4% verification pass rate (95% CI 93.4–99.0%), with 98.0% of requests served at or above
> their required model tier.**
> 1,000 labelled prompts, mock models priced like real tiers. Read the
> [case study](docs/case-study.md) for what that does and does not prove.

## Architecture

```mermaid
flowchart LR
    C[Client services] -->|POST /v1/chat| GW[Gateway]
    GW --> RT[Router<br/>3 tiers, rules-v2]
    GW --> BS[Budgets<br/>reserve then settle]
    BS -->|atomic Lua| RD[(Redis<br/>counters + stream)]
    BS --> PG[(PostgreSQL<br/>source of truth)]
    GW --> AD[Provider adapters<br/>OpenAI · Anthropic · Ollama · mock]
    GW -->|post-call checks| CS[Cascade<br/>one tier up]
    GW -.sample.-> RD
    RD -.-> WK[Verification worker<br/>reference model + judge]
    WK --> PG
    GW -.audit.-> PG
    PG --> AN[Analytics API] --> DB[Streamlit dashboard]
```

![Dashboard overview](docs/images/dashboard-overview.png)
*(Screenshot placeholder: add `docs/images/dashboard-overview.png`.)*

## Quickstart

Everything in containers (needs Docker Desktop):

```powershell
git clone https://github.com/Abhigyanv23/llm-spend-control.git
cd llm-spend-control
docker compose --profile full up -d --build      # Postgres, Redis, migrations, API, worker, dashboard
docker compose exec gateway python scripts/seed_demo_data.py    # optional: 45 days of demo traffic
```

API: **http://127.0.0.1:8000/docs** · Dashboard: **http://localhost:8501**

Local development (Windows PowerShell; Linux/Mac use the same Python commands):

```powershell
.\scripts\dev.ps1 setup      # venv, dependencies, .env
.\scripts\dev.ps1 up         # Postgres + Redis in Docker
.\scripts\dev.ps1 migrate    # alembic upgrade head
.\scripts\dev.ps1 seed       # example budgets + demo data
.\scripts\dev.ps1 serve      # API (then: worker, dashboard in other terminals)
```

## Features

| Area | What it does | Phase |
|---|---|---|
| Gateway | One canonical request/response schema; adapters for OpenAI, Anthropic, Ollama and a mock; normalised, retryable-aware errors | 1 |
| Cost tracking | Every request audited in PostgreSQL; exact money (`Decimal`, `NUMERIC(14,8)`, integer nano-dollars in Redis) | 2 |
| Budgets | Daily/monthly limits per team and feature; atomic **reserve-then-settle** in Redis (Lua); 80% warnings, 100% blocks (`402`), audited overrides; reconciliation from Postgres | 2 |
| Routing | 3-tier complexity routing (`rules-v2` classifier with confidence and reasons); per-feature rules; budget-aware downgrade; savings vs the strongest model | 3, 6 |
| Quality | Deterministic sampling to a Redis Streams queue; worker with reference model + similarity/LLM judge; routing misses as labelled data; synchronous cascade on visible failures; verification on its own budget | 4 |
| Dashboard | Analytics API (spend, projections, burn-down, gross vs net savings, Wilson intervals, latency percentiles); Streamlit dashboard | 5 |
| Evidence | 1,000-prompt labelled workload, A/B/C simulation, reproducible reports, held-out classifier evaluation | 6 |
| Ops | Docker image + compose profiles, `dev.ps1`, GitHub Actions (ruff, pytest on 3.11/3.13, smoke tests on Postgres + Redis) | 6 |

## Results (Phase 6 simulation)

| Mode | Cost / 1,000 requests | Savings vs all-strongest | Served ≥ required tier |
|---|---|---|---|
| A: all on the strongest model | $1.02 | – | 100% |
| B: routing only | $0.36 | 65.5% | 97.7% |
| C: routing + verification + escalation | $0.62 | 63.7% gross · 40.3% net | 98.0% |

- Held-out classifier accuracy: **83.4% → 92.4%** (`rules-v1` → `rules-v2`); under-routing 23 → 6 of 301.
- Escalation rescued all 23 genuinely broken cheap answers.
- Verification sample rate 5% / 10% / 25% / 50% → net savings 55.1% / 40.3% / 28.0% / 12.3%.

Full report: [`reports/final-v2/report.md`](reports/final-v2/report.md) · rebuild it with
`python scripts/build_report.py final-v2`.

## Running things

```powershell
python -m pytest                                   # 275 tests (fakeredis + SQLite, no Docker)
python -m ruff check .                             # lint
python scripts/smoke_test.py                       # smoke tests need the API running:
python scripts/smoke_routing.py                    #   gateway + budgets, routing,
python scripts/smoke_quality.py                    #   escalation + queue + worker,
python scripts/smoke_dashboard.py                  #   analytics + headless dashboard
python scripts/run_simulation.py --sweep 0.05,0.25,0.5        # A/B/C on 1,000 prompts
python scripts/build_report.py <run_id>                        # report + charts
python scripts/evaluate_classifier.py --split test --compare  # held-out rules-v1 vs rules-v2
```

Example request (PowerShell):

```powershell
$body = @{ team_id = "search"; feature = "summarize"
           messages = @(@{ role = "user"; content = "Hello gateway" }) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri http://127.0.0.1:8000/v1/chat -Method Post -ContentType "application/json" -Body $body
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | Enable real providers |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama |
| `MODEL_REGISTRY_PATH` | `config/models.yaml` | Models, tiers, prices |
| `REQUEST_TIMEOUT_S` | `60` | Upstream call timeout |
| `DATABASE_URL` | `postgresql+asyncpg://spend:spend_dev_password@127.0.0.1:5432/spend` | Postgres (source of truth) |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Budget counters and verification stream |
| `REDIS_TIMEOUT_S` | `0.5` | API's Redis timeout (fast fail-open/closed decisions) |
| `BUDGET_WARN_THRESHOLD` | `0.8` | Warning threshold |
| `BUDGET_FAIL_MODE` | `open` | `open`: allow + warn if Redis is down; `closed`: `503` |
| `RECONCILE_ON_STARTUP` | `true` | Rebuild Redis counters from Postgres at startup |
| `ROUTING_CONFIG_PATH` / `ROUTING_PROFILE` | `config/routing.yaml` / `dev` | Routing policy; `dev` = mock per tier, `production` = real providers |
| `QUALITY_CONFIG_PATH` / `VERIFY_ENABLED` | `config/quality.yaml` / `true` | Sampling, verification, escalation, privacy |
| `WORKER_CONCURRENCY` / `WORKER_CONSUMER_NAME` | `4` / `<host>-<pid>` | Verification worker |
| `ANALYTICS_CACHE_TTL_S` / `ANALYTICS_MAX_WINDOW_DAYS` | `30` / `366` | Analytics API |
| `GATEWAY_URL` | `http://127.0.0.1:8000` | Where the dashboard and scripts find the API |
| `GATEWAY_PORT` / `DASHBOARD_PORT` / `POSTGRES_PORT` / `REDIS_PORT` | `8000` / `8501` / `5432` / `6379` | Host ports published by compose |

Model names and prices in `config/models.yaml` are illustrative: verify them against each provider's
pricing page before relying on cost numbers.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: command not found` | Install Docker Desktop (Windows Home needs WSL 2), start it, open a **new** terminal |
| `ModuleNotFoundError` / wrong Python | Activate the venv; `(Get-Command python).Source` should point into `.venv` |
| `can't open file ...` / `No 'script_location'` | Run from the project folder (where `alembic.ini` is) |
| `port is already allocated` | Change `POSTGRES_PORT` / `GATEWAY_PORT` / `DASHBOARD_PORT` in `.env` |
| `password authentication failed` | The password only applies when the volume is created: `docker compose down -v` (deletes data) |
| `relation ... does not exist` / `column ... does not exist` | `python -m alembic upgrade head` |
| Every request `503 budget_unavailable` | Redis/Postgres down with `BUDGET_FAIL_MODE=closed` |
| `QualityConfigError: No usable reference model` | `production` profile without `ANTHROPIC_API_KEY`: add it, use `dev`, or `VERIFY_ENABLED=false` |
| Verifications all `skipped` | The `quality-verifier` budget is spent: raise it or lower sample rates |
| Dashboard can't reach the API / empty charts | Start the API; set `GATEWAY_URL`; `python scripts/seed_demo_data.py` |
| Simulation: `run id ... was already used` | Pick a new `--run-id` (team ids and daily budgets embed it) |

More: [Phase 2](docs/phases/phase-2-budgets.md) · [Phase 4](docs/phases/phase-4-quality.md) · [Phase 5](docs/phases/phase-5-dashboard.md) notes.

## Project layout

```
app/            gateway, budgets, routing, quality (queue, worker, judges), analytics, API routers
dashboard/      Streamlit dashboard (reads only from the analytics API)
config/         models.yaml, routing.yaml, quality.yaml
migrations/     Alembic migrations 0001-0003
scripts/        smoke tests, seeding, simulation, report builder, classifier evaluation, dev.ps1
data/           workload.jsonl (1,000 labelled prompts)
reports/        simulation runs (report, summary, charts, compressed results)
tests/          pytest suite
docs/           architecture, API, case study, interview notes, per-phase design/theory/changes
```

## Known limitations

- **No authentication**: anyone who can reach the API can change budgets and read usage.
- **Mock-model results**: costs follow real price ratios; answer quality is simulated.
- The keyword classifier is English-only; the dataset is synthetic and self-labelled.
- Analytics run on the transactional database; reconciliation assumes a single gateway instance.
- Per-phase lists: [2](docs/phases/phase-2-budgets.md#known-limitations) · [3](docs/phases/phase-3-routing.md#known-limitations) · [4](docs/phases/phase-4-quality.md#known-limitations) · [5](docs/phases/phase-5-dashboard.md#known-limitations) · [6](docs/phases/phase-6-simulation.md#known-limitations)

## Documentation

- [Case study](docs/case-study.md) · [Interview notes](docs/interview-notes.md)
- [Architecture](docs/architecture.md) · [API reference](docs/api.md) · [Changelog](CHANGELOG.md)
- Design notes: [1](docs/phases/phase-1-gateway.md) · [2](docs/phases/phase-2-budgets.md) · [3](docs/phases/phase-3-routing.md) · [4](docs/phases/phase-4-quality.md) · [5](docs/phases/phase-5-dashboard.md) · [6](docs/phases/phase-6-simulation.md)
- Theory notes: [2](docs/notes/phase-2-theory.md) · [3](docs/notes/phase-3-theory.md) · [4](docs/notes/phase-4-theory.md) · [5](docs/notes/phase-5-theory.md) · [6](docs/notes/phase-6-theory.md)
- Change records: [2](docs/phases/phase-2-changes.md) · [3](docs/phases/phase-3-changes.md) · [4](docs/phases/phase-4-changes.md) · [5](docs/phases/phase-5-changes.md) · [6](docs/phases/phase-6-changes.md)

## Authors

Abhigyan Varma

Enrique Dias
