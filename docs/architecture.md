# Architecture

## Current state (after Phase 4)

```mermaid
flowchart LR
    C[Client service] -->|POST /v1/chat<br/>+ X-Budget-Override?| API[FastAPI app]
    API --> GW[Gateway pipeline]
    GW --> RT[Router]
    RT --> RC[(Routing policy<br/>routing.yaml)]
    RT --> CL[Classifier<br/>rules-v1]
    GW --> QP[(Quality policy<br/>quality.yaml)]
    GW --> REG[(Model registry<br/>models.yaml)]
    RT --> REG
    GW --> BS[BudgetService]
    BS -->|policies| PG[(PostgreSQL<br/>source of truth)]
    BS -->|atomic Lua<br/>reserve / settle| RD[(Redis<br/>counters + stream)]
    GW --> AD{Adapter lookup<br/>by provider}
    AD --> OA[OpenAI adapter] --> OAPI[OpenAI API]
    AD --> AN[Anthropic adapter] --> AAPI[Anthropic API]
    AD --> OL[Ollama adapter] --> OLL[Local Ollama]
    AD --> MK[Mock adapter]
    GW -->|response + routing / escalation /<br/>quality metadata + X-Budget-Warning| API
    API -.->|background task| AU[AuditLogger] -.->|INSERT request_logs| PG
    API -.->|background task: XADD<br/>if sampled| RD
    WK[Verification worker<br/>python -m app.worker] -.->|XREADGROUP / XACK<br/>XAUTOCLAIM| RD
    WK -.->|reference call + judge| AD
    WK -.->|reserve / settle<br/>verifier budget| BS
    WK -.->|INSERT verifications,<br/>routing_misses| PG
    PG -.->|startup reconciliation<br/>SUM request + verification spend| RD
    UB[/v1/usage, /v1/budgets/] --> PG
    UB --> RD
    RA[/v1/routing, /v1/route/preview/] --> RT
    QA[/v1/quality, /misses, /queue/] --> PG
    QA --> RD
```

Solid arrows are on the request's critical path; dotted arrows happen after the response, in
the worker process, or at startup. `routing.yaml` and `quality.yaml` are loaded and validated
once at startup by `app/bootstrap.py`, the composition root shared by the API and the worker.

## Request lifecycle

1. **Validate**: Pydantic validates the body against `ChatRequest`. A bad body returns 422 and
   is still audited (`status = validation_error`).
2. **Route**: the `Router` picks a model and tier (see [Routing decision](#routing-decision)).
   An explicit `model` is honoured; an unknown one returns 400. The decision carries cheaper
   `fallbacks` if the feature allows budget downgrade.
3. **Pre-call escalation** (Phase 4): a *routed* `high`/`critical` request whose classifier
   confidence is below `escalation.pre_call.confidence_below` starts one tier higher (never
   above `max_tier`); the original model becomes the first budget fallback.
4. **Context check**: estimated input tokens (~4 chars/token + 4 per message) + `max_tokens`
   must fit in the model's `max_context`.
5. **Estimate worst-case cost**: estimated input tokens at the input price + **all** of
   `max_tokens` at the output price, as a `Decimal`.
6. **Reserve**: `BudgetService.reserve()` atomically checks `spent + reserved + estimate`
   against every limit (team/feature × day/month) in one Lua script and holds the estimate.
   - ≥ 100% → **downgrade** to the next cheaper fallback, else `402 budget_exceeded` /
     `402 override_required` (a valid `X-Budget-Override` is allowed and flagged).
   - ≥ 80% → allowed with warnings and a deduplicated `budget_alerts` row.
   - Redis/Postgres unreachable → `BUDGET_FAIL_MODE` decides: allow unchecked, or `503`.
7. **Dispatch** to the provider adapter; latency timed with `perf_counter`.
8. **Settle**: release the hold, add the actual cost. Any exception after step 6 releases the
   hold in `finally` instead.
9. **Post-call checks and cascade** (Phase 4): the answer is checked for *visible* failures
   (empty, refusal, `finish_reason` length/max_tokens, invalid JSON when JSON was requested).
   On a failure, if the request was routed and a higher allowed tier exists, steps 5–8 repeat
   once on the next tier (`max_escalations`). Blocked or failed escalations keep the original
   answer and add a note. See [Escalation cascade](#escalation-cascade).
10. **Sample** (Phase 4): a deterministic hash of the request id decides whether a stronger
    model should double-check this answer later (routed, tier < 3, not escalated).
11. **Respond**: canonical `ChatResponse` (money as fixed-point strings) with
    `metadata.routing`, `metadata.escalation` and `metadata.quality`, plus `X-Budget-Warning`.
12. **After the response** (background tasks): the `request_logs` row is inserted (one row,
    summed cost of all attempts), and a sampled job is `XADD`ed to the verification stream.
    Neither can fail the request. Errors carry their audit record to the error handler.

## Routing decision

```mermaid
flowchart TD
    A[Request] --> B{model given?}
    B -->|yes| X[explicit: use it as-is]
    B -->|no| D{feature pinned?}
    D -->|yes| P[pinned: use pin_model]
    D -->|no| E[Classifier: tier, confidence, reasons]
    E --> F[Clamp tier to feature min..max<br/>and priority floor]
    F --> G{usable candidate in tier?<br/>available · fits context · has capabilities}
    G -->|yes| H[Chosen model]
    G -->|no, tier < max_tier| I[Try next tier] --> G
    G -->|no, at max_tier| J{only the context<br/>is too small?}
    J -->|yes| K[400 context_too_long]
    J -->|no| L[503 no_route]
    H --> M[Fallbacks: first usable model in each<br/>cheaper tier down to min_tier<br/>if budget_downgrade is on]
    M --> N{high/critical and<br/>confidence < 0.6?}
    N -->|yes, tier < max_tier| O[Pre-call escalation:<br/>start one tier higher]
    N -->|no| Q[Final decision]
    O --> Q
```

Classifier (`rules-v1`) signals, matched in the system prompt + latest user message:

| Signal | Effect | Confidence |
|---|---|---|
| Risk keyword (`contract`, `medical`, ...) | tier 3 | 0.9 |
| Keywords from one tier only | that tier | 0.85 |
| Keywords from several tiers | highest tier | 0.65 |
| No keywords | tier 1 (optimistic default) | 0.5 |
| Code present / input > `long_context_tokens` | at least tier 2 | unchanged |

## Escalation cascade

```mermaid
flowchart TD
    A[Answer from attempt N] --> B{post-call check fails?<br/>empty · refusal · truncated · invalid_json}
    B -->|no| Z[Return this answer]
    B -->|yes| C{routed, escalations left,<br/>tier < max_tier,<br/>higher-tier model usable?}
    C -->|no| Y[Return this answer<br/>+ escalation.note]
    C -->|yes| D[Reserve next tier]
    D -->|blocked| W[Return this answer<br/>+ escalation.blocked]
    D -->|reserved| E[Call next tier, settle its cost]
    E -->|provider error| V[Release hold, return this answer<br/>+ escalation.note]
    E -->|ok| A
```

The audit row stores `model` = the model that produced the returned answer and `cost_usd` =
the sum of all attempts; `metadata.escalation.attempts` lists each attempt with its cost and
the check it failed. Escalated requests are not sampled for async verification.

## Reserve → call → settle → log

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant G as Gateway
    participant RT as Router
    participant B as BudgetService
    participant P as Postgres
    participant R as Redis
    participant L as LLM provider

    C->>G: POST /v1/chat (team, feature, priority, max_tokens)
    G->>RT: route(request)
    RT-->>G: model + fallbacks (cheaper allowed tiers)
    loop routed model, then each fallback
        G->>G: estimate worst case = input_est × in_price + max_tokens × out_price
        G->>B: reserve(estimate)
        B->>P: SELECT policies (team, feature)
        B->>R: EVALSHA reserve.lua (4 counters, limits, estimate)
        Note over R: atomic: read spent + reserved,<br/>compare to limits, INCRBY reserved
        alt allowed
            R-->>B: allowed = 1
            B-->>G: Reservation (warnings?)
        else blocked and a cheaper fallback exists
            R-->>B: allowed = 0 (nothing held)
            B-->>G: 402, try next fallback
        else blocked, no fallback left
            B-->>G: raise 402
            G-->>C: 402 budget_exceeded / override_required
        end
    end
    G->>L: complete()
    alt provider success
        L-->>G: tokens used
        G->>R: EVALSHA settle.lua (release hold, add actual cost)
        G->>G: post-call checks (cascade if needed), savings, sampling decision
        G-->>C: 200 + metadata + X-Budget-Warning?
    else provider error / exception
        G->>R: settle.lua with actual = 0 (finally block)
        G-->>C: 4xx/5xx provider_error
    end
    G--)P: background: INSERT request_logs (every outcome)
```

## Sample → enqueue → worker → reference → judge → store → ack

```mermaid
sequenceDiagram
    autonumber
    participant G as Gateway (API)
    participant S as Redis Stream<br/>quality:verify
    participant W as Worker
    participant B as BudgetService
    participant RM as Reference model (tier 3)
    participant J as Judge
    participant P as Postgres

    G->>G: sampled = SHA-256(request_id) < rate<br/>(50% if confidence < 0.6, else 10%)
    G--)S: background: XADD MAXLEN ~ 10000 (job: messages, cheap output, model, tier, features)
    loop every batch (≤ WORKER_CONCURRENCY jobs)
        W->>S: XAUTOCLAIM jobs idle > job_timeout_s (crashed/failed workers)
        W->>S: XREADGROUP > (new jobs) → job enters the Pending Entries List
        W->>W: delivery count > max_attempts? → dead-letter stream + XACK
        W->>P: already verified? (idempotency pre-check) → XACK, skip
        W->>B: reserve worst case (reference + judge) on quality-verifier
        alt verifier budget exhausted
            W->>P: INSERT verifications (verdict = skipped)
        else reserved
            W->>RM: same messages, temperature 0
            RM-->>W: reference answer
            W->>J: grade cheap answer vs reference
            J-->>W: pass / fail / inconclusive + score + reason
            W->>B: settle(actual reference + judge cost)
            W->>P: INSERT verifications (+ routing_misses if fail), one transaction,<br/>UNIQUE request_id
        end
        W->>S: XACK (only after the row is committed)
    end
    Note over W,S: Any exception before XACK leaves the job pending:<br/>redelivered after job_timeout_s (at-least-once)
```

## Redis key layout

```
budget:{scope}:{scope_id}:day:{YYYY-MM-DD}:spent       integer nano-dollars
budget:{scope}:{scope_id}:day:{YYYY-MM-DD}:reserved
budget:{scope}:{scope_id}:month:{YYYY-MM}:spent
budget:{scope}:{scope_id}:month:{YYYY-MM}:reserved
alert:budget:{scope}:{scope_id}:{period}:{key}:{threshold}   SET NX dedup marker
quality:verify                                           stream of verification jobs
                                                         (consumer group "verifiers")
quality:verify:dead                                      dead-letter stream (reason, attempts)
```

Budget TTL = time until the period resets + 24 h grace. Counters are kept for **every** team
and feature (not only those with a policy). Streams are capped with approximate `MAXLEN`.

## Processes

| Process | Command | Scales by | Holds |
|---|---|---|---|
| API | `python -m uvicorn app.main:app` | more uvicorn workers/instances | no state (Postgres + Redis) |
| Verification worker | `python -m app.worker` | more worker processes: the consumer group splits jobs | only in-flight jobs (pending in Redis) |

Both build their dependencies with `build_core()` in `app/bootstrap.py`; the worker never
imports the web app.

## Key design principles

| Principle | Where it shows up |
|---|---|
| Single choke point | All LLM traffic passes through `Gateway.handle()`, the home of routing, budgets and escalation |
| Canonical schema / anti-corruption layer | Provider quirks are confined to adapters; the core only sees `ChatRequest`/`ProviderResult` |
| Adapter pattern + Open/Closed | New provider = new adapter file + one line in `build_adapters()` |
| Strategy pattern | Classifier behind `classify()`; judges behind `Judge.judge()` (similarity ↔ LLM) |
| Mechanism vs policy | Router and gateway are code; tiers, escalation rules, sampling rates live in YAML |
| Config as data | Prices, tiers, routing and quality rules in YAML; limits in `budget_policies` |
| Fail fast | `routing.yaml` and `quality.yaml` are validated at startup; a bad config stops the process |
| Centralised cost logic | Adapters report tokens only; cost is computed once from the registry, as `Decimal` |
| Uniform errors | Every failure returns a `GatewayError` shape |
| Reserve-then-settle | Enforce before spending, account after knowing; applies to escalation attempts and verifications too |
| Graceful degradation | Budget block → downgrade; failed/blocked escalation → original answer; queue down → no verification, request unaffected |
| At-least-once + idempotency | Ack after commit; UNIQUE `verifications.request_id` makes redelivery harmless |
| Separate worker process | Slow verification never competes with user requests; scales and deploys independently |
| Explainability | Routing reasons, escalation attempts and sampling decisions are stored per request |
| Source of truth vs cache | Postgres is authoritative; Redis counters are rebuildable (request + verification spend) |
| Data minimisation | Stored prompts are optional and capped; listings show previews only |
| Dependency injection | `build_core()` wires everything once; tests inject SQLite + fakeredis |
| Layering | `api/` (HTTP) → `gateway` (orchestration) → `routing/`, `budgets/`, `quality/`, `audit` (domain) → `db/`, Redis (infrastructure) |

## Planned evolution

| Phase | Adds |
|---|---|
| 5 | Dashboard over `request_logs`, `verifications`, `routing_misses` and `budget_alerts`: spend, savings, miss rate, escalation rate |
| 6 | Simulated 1,000-request workload through the full pipeline (incl. verification); savings report net of quality overhead |
