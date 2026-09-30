# Architecture

## Current state (after Phase 2)

```mermaid
flowchart LR
    C[Client service] -->|POST /v1/chat<br/>+ X-Budget-Override?| API[FastAPI app]
    API --> GW[Gateway pipeline]
    GW --> REG[(Model registry<br/>models.yaml)]
    GW --> BS[BudgetService]
    BS -->|policies| PG[(PostgreSQL<br/>source of truth)]
    BS -->|atomic Lua<br/>reserve / settle| RD[(Redis<br/>nano-dollar counters)]
    GW --> AD{Adapter lookup<br/>by provider}
    AD --> OA[OpenAI adapter] --> OAPI[OpenAI API]
    AD --> AN[Anthropic adapter] --> AAPI[Anthropic API]
    AD --> OL[Ollama adapter] --> OLL[Local Ollama]
    AD --> MK[Mock adapter]
    GW -->|response + X-Budget-Warning| API
    API -.->|background task| AU[AuditLogger] -.->|INSERT request_logs| PG
    PG -.->|startup reconciliation<br/>SUM cost_usd| RD
    UB[/v1/usage, /v1/budgets/] --> PG
    UB --> RD
```

Solid arrows are on the request's critical path; dotted arrows happen after the response or
at startup.

## Request lifecycle

1. **Validate**: Pydantic validates the body against `ChatRequest`. A bad body returns 422 and
   is still audited (`status = validation_error`).
2. **Resolve model**: use `request.model`, or fall back to the registry default. Unknown model returns 400.
3. **Context check**: estimated input tokens (~4 chars/token + 4 per message) + `max_tokens`
   must fit in the model's `max_context`.
4. **Estimate worst-case cost**: estimated input tokens at the input price + **all** of
   `max_tokens` at the output price, as a `Decimal`.
5. **Reserve**: `BudgetService.reserve()` loads the team and feature policies from Postgres,
   then runs one Lua script in Redis that, atomically, checks `spent + reserved + estimate`
   against every limit (team/feature × day/month) and, if allowed, adds the estimate to each
   `:reserved` counter.
   - ≥ 100% of a limit → `402 budget_exceeded`, or `402 override_required` for high/critical
     priority without an override. A valid override is allowed and flagged.
   - ≥ 80% → allowed with warnings (header + metadata) and a deduplicated `budget_alerts` row.
   - Redis/Postgres unreachable → `BUDGET_FAIL_MODE` decides: allow unchecked, or `503`.
6. **Dispatch**: the adapter for `spec.provider` is called; latency is timed with `perf_counter`.
7. **Settle**: a second Lua script subtracts the hold from `:reserved` and adds the actual cost
   to `:spent`. On any exception after step 5, `finally` **releases** the hold instead (adds nothing).
8. **Respond**: canonical `ChatResponse` (money as fixed-point strings) plus `X-Budget-Warning`
   when applicable.
9. **Audit** (after the response is sent): a background task inserts the `request_logs` row.
   Errors carry their audit record to the error handler, which attaches the same write to the
   error response.

## Reserve → call → settle → log

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as Gateway
    participant B as BudgetService
    participant P as Postgres
    participant R as Redis
    participant L as LLM provider

    C->>G: POST /v1/chat (team, feature, priority, max_tokens)
    G->>G: estimate worst case = input_est × in_price + max_tokens × out_price
    G->>B: reserve(estimate)
    B->>P: SELECT policies (team, feature)
    B->>R: EVALSHA reserve.lua (4 counters, limits, estimate)
    Note over R: atomic: read spent + reserved,<br/>compare to limits, INCRBY reserved
    alt limit reached and no valid override
        R-->>B: allowed = 0
        B-->>G: raise 402
        G-->>C: 402 budget_exceeded / override_required
    else allowed
        R-->>B: allowed = 1 (+ counters seen)
        B-->>G: Reservation (warnings?)
        G->>L: complete()
        alt provider success
            L-->>G: tokens used
            G->>R: EVALSHA settle.lua (release hold, add actual cost)
            G-->>C: 200 + X-Budget-Warning?
        else provider error / exception
            G->>R: settle.lua with actual = 0 (finally block)
            G-->>C: 4xx/5xx provider_error
        end
    end
    G--)P: background: INSERT request_logs (every outcome)
```

## Redis key layout

```
budget:{scope}:{scope_id}:day:{YYYY-MM-DD}:spent       integer nano-dollars
budget:{scope}:{scope_id}:day:{YYYY-MM-DD}:reserved
budget:{scope}:{scope_id}:month:{YYYY-MM}:spent
budget:{scope}:{scope_id}:month:{YYYY-MM}:reserved
alert:budget:{scope}:{scope_id}:{period}:{key}:{threshold}   SET NX dedup marker
```

TTL = time until the period resets + 24 h grace. Counters are kept for **every** team and
feature (not only those with a policy), so a policy created mid-day immediately sees the
spend so far.

## Key design principles

| Principle | Where it shows up |
|---|---|
| Single choke point | All LLM traffic passes through `Gateway.handle()`, the home of budgets and (next) routing |
| Canonical schema / anti-corruption layer | Provider quirks are confined to adapters; the core only sees `ChatRequest`/`ProviderResult` |
| Adapter pattern + Open/Closed | New provider = new adapter file + one line in `build_adapters()` |
| Config as data | Prices, tiers and limits live in YAML and in the `budget_policies` table, not code |
| Centralised cost logic | Adapters report tokens only; cost is computed once from the registry, as `Decimal` |
| Uniform errors | Every failure, budget blocks included, returns a `GatewayError` shape |
| Reserve-then-settle | Enforce before spending, account after knowing; atomic in Redis |
| Source of truth vs cache | Postgres is authoritative; Redis counters are rebuildable from it |
| Dependency injection | `create_app()` wires engine/Redis/services once; tests inject SQLite + fakeredis |
| Layering | `api/` (HTTP) → `gateway` (orchestration) → `budgets/`, `audit` (domain) → `db/`, Redis (infrastructure) |

## Planned evolution

| Phase | Adds to the pipeline |
|---|---|
| 3 | Router replaces the "default model" step with complexity-tier selection; the budget estimate feeds routing (downgrade instead of block) |
| 4 | Post-response sampling to a verifier queue; escalation for high-risk requests |
| 5 | Dashboard reading from `request_logs` and `budget_alerts` |
| 6 | Simulated workload through the full pipeline |
