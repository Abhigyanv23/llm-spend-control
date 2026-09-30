# Phase 2: Cost Tracking and Budgets

## Goal
Every request is logged to a durable audit trail, and every team and feature has daily and
monthly budgets enforced in real time, **before** money is spent.

The core problem: a budget must be checked before the provider call, but the true cost is
only known after the response. Solved with **reserve-then-settle**.

## What was built
- PostgreSQL audit log (`request_logs`) covering every outcome: success, provider error,
  validation error, budget block, internal error.
- Budget policies per team and per feature (`budget_policies`), daily and/or monthly.
- Redis counters in integer nano-dollars, updated by two Lua scripts (reserve, settle).
- Warning (80%) / block (100%) / override (high and critical priority) behaviour, with
  deduplicated alerts (`budget_alerts`).
- Reconciliation that rebuilds Redis from Postgres (startup + admin endpoint).
- Fail-open / fail-closed behaviour when Redis or Postgres is down.
- Usage and budget endpoints; Docker Compose infra; Alembic migrations; pytest suite.

## Design decisions

**Reserve-then-settle (like a hotel card pre-authorisation).** At check-in the hotel holds
$500 on your card: the money is unavailable to you but not yet charged. At checkout the hold
is replaced by the real bill. Here, before the call we hold the *worst case* (estimated input +
all of `max_tokens` at the output price). After the call we release the hold and book the
actual cost. On failure we release and book nothing. Because the hold is visible to every
other request immediately, 50 concurrent requests can't all squeeze into the last $1 of budget.

**Why the worst case?** Output length is unknown until generation ends. `max_tokens` is the
only hard upper bound the caller controls. Reserving it guarantees that, as long as the input
estimate is right, a request can never push spend past the limit. Side effect: large
`max_tokens` values make a request look expensive, which nudges callers to set realistic limits.

**Atomic check-and-reserve with Lua.** The naive version is `GET spent`, `GET reserved`,
compare, `INCRBY reserved`. Between the GET and the INCRBY another request can do the same GET,
and both see room that only exists once (a *time-of-check to time-of-use* race). A Redis Lua
script runs as one indivisible unit, so check and reserve cannot interleave. `MULTI/EXEC`
alone would not work here: it queues commands without letting you branch on a value read
inside the transaction. (`WATCH` + `MULTI` can, but it retries under contention.)
`tests/test_budget_store.py` shows the naive version admitting all 50 racers while the Lua
version admits exactly 10.

**One round trip for four counters.** A request touches team×day, team×month, feature×day,
feature×month. The script checks all four and reserves on all four, or none, in one call.
Counters without a limit are still reserved and settled, so spend is tracked for every team
and feature. A policy created mid-day therefore sees today's spend immediately.

**Strictest outcome wins.** Block > override required > warning > ok. The error reports the
most-exceeded limit and lists all exceeded ones in `limits_exceeded`.

**Money: Decimal, NUMERIC(14,8), nano-dollars.** Floats are binary fractions (0.1 + 0.2 ≠ 0.3)
and the error accumulates over millions of tiny costs. Python uses `Decimal`; Postgres stores
`NUMERIC(14,8)` (exact, up to $999,999.99999999); Redis has no decimal type, so it stores
integers of 10⁻⁹ USD. `INCRBY` on integers is exact, and one token of a $0.10/MTok model
(1×10⁻⁷ USD = 100 nano-dollars) is still a whole number. API responses serialise money as
fixed-point strings (`str(Decimal)` would print `0E-8`).

**Postgres = source of truth, Redis = cache.** Postgres gives durability and ACID transactions
plus SQL for audits and reporting. Redis gives sub-millisecond atomic counters on the hot path.
Redis can lose or drift from the truth (restart without persistence, crash between reserve and
settle, a failed settle), so `reconcile_counters()` recomputes `SUM(cost_usd)` per team and
feature for the current day and month and overwrites the counters inside one `MULTI/EXEC`.

**Audit write in a background task.** The client gets its response before the INSERT runs,
so Postgres latency or a slow disk never adds to request latency. Trade-offs: (1) eventual
consistency, since `/v1/usage` may lag the response by milliseconds; (2) if the process dies
after responding but before the INSERT, that row is lost. For a billing-grade ledger you would
write inline (or to a durable queue) in the same step as settlement. Redis counters are updated
inline, so enforcement itself is never eventually consistent.

**Errors carry their audit record.** FastAPI discards route `BackgroundTasks` when the route
raises, so the gateway attaches the `AuditRecord` to the exception and the error handler
schedules the write on the error response. Blocked and failed requests are never lost.

**`try/finally` + `asyncio.shield`.** Any exception after the reservation (provider error,
timeout, bug, client disconnect/cancellation) runs the `finally` that releases the hold.
`shield` lets the release finish even if the task is being cancelled.

**Settle against the reservation's keys.** A request that reserved at 23:59:59 UTC settles
into that day's counter even if it finishes after midnight. TTLs are "time until reset + 24 h"
so that key still exists.

**Fail-open vs fail-closed.** If the budget system is down, either let traffic through
(availability first: the product keeps working, spend is unchecked for a while, and
reconciliation catches up) or reject it (cost control first: no surprise bills, but an outage
in Redis becomes an outage of every LLM feature). Default is `open` with a loud warning log
and an `X-Budget-Warning: budget-check-unavailable` header. Regulated or hard-capped
environments would choose `closed`.

**HTTP 402.** Not 429 (rate limit: retry soon) or 403 (not permitted). The caller is allowed
and not too fast; they are out of money until `resets_at`.

**Dependency injection.** `create_app()` builds the engine, Redis client, `BudgetService`,
`AuditLogger` and `Gateway` once and passes them in; nothing constructs its own dependencies.
Tests inject SQLite + fakeredis with zero code changes, the same way Phase 1 injects adapters.

**Seed script, not a data migration.** Migrations hold schema (and data every environment
needs). Demo budgets are environment-specific, so they live in an idempotent script.

## Known limitations
- **No auth**: anyone can change budgets or read usage.
- **Input estimate is a heuristic** (~4 chars/token + 4 per message). Code or non-English text
  can exceed it, so a request can overshoot a limit by the estimation error (settle books the
  real cost regardless). Fix: a real tokenizer (Phase 3) or a safety margin.
- **Reconciliation resets in-flight holds.** Safe for one instance at startup; several instances
  need a distributed lock or per-instance hold tracking.
- **Redis Cluster**: the reserve script touches keys of two scopes, which may live on different
  slots. Hash tags or one script call per scope would be needed.
- **Audit rows can be lost** if the process dies between response and background INSERT.
- **Policies are read from Postgres on every request** (one indexed lookup). A short TTL cache
  would remove that from the hot path at the cost of slightly stale limits.
- **Provider-side billing of failed calls** (e.g. a timeout after generation started) is booked as $0.
- **Offset pagination** on `/v1/usage` gets slow deep into large tables; keyset pagination later.
- **Alerts are only stored and logged**; no notifications (Slack/email) yet.

## Key formulas
```
cost_usd            = (input_tokens × input_price_per_MTok + output_tokens × output_price_per_MTok) / 1,000,000
worst_case_estimate = (est_input_tokens × input_price + max_tokens × output_price) / 1,000,000
est_input_tokens    = Σ over messages (max(1, len(content) // 4) + 4)
nanos               = round_half_up(usd × 1,000,000,000)
projected           = spent + reserved + estimate          (per scope × period, in nanos)
ratio               = projected / limit                    (limit = 0 → treated as 1.0)
blocked             ⇔ ratio ≥ 1.0   (unless high/critical priority with X-Budget-Override)
warning             ⇔ BUDGET_WARN_THRESHOLD ≤ ratio < 1.0
settle              : reserved −= estimate (floored at 0); spent += actual_cost
TTL                 = seconds until period reset + 86,400
```

## Interview talking points
- "Enforce before spending, account after knowing": explain reserve-then-settle with the hotel analogy.
- Show the race: the naive GET-then-SET test admits 50/50; the Lua version admits exactly 10.
- Why nano-dollar integers in Redis and `NUMERIC` in Postgres, and why money is a string in JSON.
- Postgres is the truth, Redis is a rebuildable cache: what breaks without reconciliation.
- Fail-open vs fail-closed is a business decision; the code makes it one config flag.
- What you would change at scale: Redis Cluster hash tags, a durable audit queue, a real tokenizer, auth.
