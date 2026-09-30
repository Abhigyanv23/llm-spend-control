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
| Request audit log (PostgreSQL) | ⏳ Phase 2 |
| Per-team/feature daily & monthly budgets (Redis) | ⏳ Phase 2 |
| Complexity-based model routing | ⏳ Phase 3 |
| Async quality verification & escalation | ⏳ Phase 4 |
| Cost dashboard | ⏳ Phase 5 |
| 1,000-request simulation & savings report | ⏳ Phase 6 |

## Tech Stack

Python 3.11+ · FastAPI · httpx (async) · Pydantic v2 · PyYAML
*(Coming: PostgreSQL · Redis · scikit-learn · Celery/RQ · Streamlit · Docker Compose)*

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate                     # Linux/Mac: source .venv/bin/activate
python -m pip install -r requirements.txt
copy .env.example .env                     # Linux/Mac: cp .env.example .env
python -m uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/docs** for the interactive API.

### Smoke test

With the server running, in a second terminal:

```bash
python scripts/smoke_test.py
```

This checks the happy path (mock model) plus error handling: unknown model, missing API key,
validation error, and context-length limit.

### Example request

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
```

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `getaddrinfo failed` during `pip install` | No DNS/internet, or a proxy is required | Check `nslookup pypi.org`; use `--proxy` or another network |
| `ModuleNotFoundError` with a traceback path outside `.venv` | A global `uvicorn` ran instead of the venv's | Always run `python -m uvicorn ...` with the venv activated |
| `ImportError: cannot import name X` | File exists but doesn't define `X` (unsaved or wrong file) | Check the file's contents; save it |
| `Cannot bind parameter 'Headers'` in PowerShell | `curl` is an alias for `Invoke-WebRequest` | Use `Invoke-RestMethod` (above) or `scripts/smoke_test.py` |
| Slow installs / "file in use" errors | Project inside a OneDrive-synced folder | Move the project outside OneDrive and recreate `.venv` |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | Enables OpenAI models |
| `ANTHROPIC_API_KEY` | — | Enables Anthropic models |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server |
| `MODEL_REGISTRY_PATH` | `config/models.yaml` | Model catalogue |
| `REQUEST_TIMEOUT_S` | `60` | Upstream call timeout |

Model prices in `config/models.yaml` are illustrative. Update them from each provider's pricing page.

```
app/
├── main.py          FastAPI app, lifespan, error handler, routes
├── gateway.py       Core request pipeline
├── registry.py      Model registry + cost calculation
├── schemas.py       Canonical request/response models
├── errors.py        Normalised error types
├── config.py        Environment settings
└── providers/       One adapter per provider (Adapter pattern)
config/models.yaml   Model catalogue
scripts/             Smoke tests and utilities
docs/                Architecture, API reference, per-phase design notes
```

## Documentation

- [Architecture](docs/architecture.md)
- [API Reference](docs/api.md)
- [Changelog](CHANGELOG.md)
- Phase notes: [Phase 1: Gateway](docs/phases/phase-1-gateway.md)

## Author

Abhigyan Varma

Enrique Dias