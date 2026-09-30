# Phase 2 Theory Notes: Cost Tracking and Budgets

Study notes for the concepts behind Phase 2. Each topic: the idea, why it matters, and where it
shows up in this codebase.

---

## 1. Relational DB for the audit trail, Redis for counters

Two stores with two different jobs.

- **PostgreSQL** is a relational database: durable (data is on disk and survives crashes),
  transactional, queryable with SQL. An audit trail must never silently lose rows and must
  answer questions nobody planned for ("cost per feature per model last Tuesday"). That is
  exactly what a relational DB is for.
- **Redis** is an in-memory key-value store: every operation takes microseconds and is
  single-threaded, so each command (and each Lua script) is atomic. It is ideal for hot
  counters that every request reads and writes, and poor as a system of record (memory
  only, limited querying, persistence is optional and asynchronous).

Rule of thumb: *the hot path talks to Redis; the truth lives in Postgres.*
→ `app/db/models.py` (Postgres), `app/budgets/store.py` (Redis).

## 2. ACID vs speed

**ACID** = Atomicity (all or nothing), Consistency (constraints always hold), Isolation
(concurrent transactions don't see each other's half-done work), Durability (committed = on
disk). ACID costs latency: a Postgres commit waits for a disk flush (fsync), typically about
1–10 ms. Redis skips most of that. Writes go to memory, and persistence (AOF/RDB) happens
later or is off entirely, so it's roughly 100× faster, but a crash can lose the last moments
of writes.

Design consequence: use ACID where losing data is unacceptable (the ledger) and speed where
the data is *rebuildable* (counters). See topic 7.

## 3. Money: Decimal / NUMERIC vs float, and integer nano-dollars in Redis

- A `float` is a binary fraction. 0.1 has no exact binary representation, so
  `0.1 + 0.2 == 0.30000000000000004`. Tiny per-request costs summed millions of times
  accumulate visible errors, and money must reconcile to the cent.
- `decimal.Decimal` (Python) and `NUMERIC(p, s)` (SQL) are **exact base-10** numbers.
  `NUMERIC(14, 8)` = 14 significant digits, 8 after the point → max $999,999.99999999, smallest
  step $0.00000001.
- Redis has no decimal type; `INCRBYFLOAT` would bring the float problem back. So we store
  **integers of 10⁻⁹ USD** (nano-dollars). Integer addition is exact. One token of a cheap
  model ($0.10/MTok = 10⁻⁷ USD) is 100 nano-dollars, still an integer.
- Convert floats via `str()` first: `Decimal(0.1)` gives the float's exact binary value
  (0.1000000000000000055…), whereas `Decimal("0.1")` gives 0.1.
- Serialise money in JSON as a **string**. JSON numbers are parsed as floats by most clients.
  Also, Python's `str(Decimal)` switches to scientific notation below 10⁻⁶ (`"0E-8"`), so
  format explicitly (`format(d, "f")`).
- Trivia caught while building: Python 3.12+ `sum()` uses compensated summation for floats,
  so `sum([0.1]*10) == 1.0`, but a running `total += 0.1` still drifts.

→ `app/money.py`, `USD` type in `app/schemas.py`.

## 4. Migrations and why Alembic

A **migration** is a versioned, reviewable script that moves the database schema from one
version to the next (`upgrade`) and back (`downgrade`). Why not just `create_all()`?
- `create_all()` only creates missing tables. It can't add a column to an existing table or
  change a type, and it keeps no history.
- Migrations make schema changes **repeatable** (same result on laptop, CI and prod),
  **ordered** (each revision points to its parent), and **reviewable** in git.
- **Alembic** is SQLAlchemy's migration tool. It tracks the current version in an
  `alembic_version` table and can *autogenerate* a draft by diffing models against the DB.
  Always review drafts, because autogenerate misses things like renames and some constraint changes.
- `alembic check` (used while building this phase) fails if models and migrations disagree.

→ `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_*.py`.

## 5. Race conditions and atomicity

A **race condition** is a bug whose outcome depends on the timing of concurrent operations.
Classic form: *check-then-act* (time-of-check to time-of-use, TOCTOU).

```
Request A: GET reserved → 0       Request B: GET reserved → 0
Request A: 0 + 100 < limit ✓      Request B: 0 + 100 < limit ✓
Request A: SET reserved = 100     Request B: SET reserved = 100   ← A's hold is lost
```
Both passed a check that only one should have passed, and one update was overwritten (a
*lost update*). With async code this happens even in one process: every `await` is a point
where another request can run.

Fixes:
- **Atomic single commands** (`INCRBY`) fix the lost update but not the check.
- **`MULTI/EXEC`** queues commands and runs them together, but you can't read a value and
  branch on it *inside* the transaction.
- **`WATCH` + `MULTI`** is optimistic locking: the transaction aborts if a watched key changed,
  and you retry. It works, but it retries a lot under contention.
- **Lua scripts** (`EVAL`/`EVALSHA`) run server-side as one indivisible unit, so you can read,
  decide and write with nothing interleaving. ✔ Used here.

→ `app/budgets/lua.py`; demonstrated in `tests/test_budget_store.py` (naive: 50/50 admitted;
Lua: exactly 10).

## 6. Reserve-then-settle (the hotel pre-authorisation)

Problem: enforce the budget *before* the call, but the cost is only known *after*.

A hotel places a **hold** on your card at check-in (money is blocked, not charged), then at
checkout **replaces** the hold with the real bill. Same here:

1. **Estimate** the worst case: input estimate + all of `max_tokens` at the output price.
2. **Reserve**: atomically check `spent + reserved + estimate` against every limit and add
   the estimate to `reserved`. Other requests see the hold immediately.
3. **Call** the provider.
4. **Settle**: `reserved -= estimate`, `spent += actual`. On failure, **release**:
   `reserved -= estimate` and add nothing.
5. Wrap steps 3–4 in `try/finally` so a hold can never leak.

Why not just check `spent < limit` and add the cost afterwards? Because 50 concurrent requests
all see the same `spent` and all pass. The hold is what makes in-flight spend visible.

→ `app/budgets/service.py`, `app/gateway.py`.

## 7. Source of truth vs cache, and reconciliation

- **Source of truth**: the authoritative copy. If they disagree, it wins. Here: `request_logs`
  in Postgres.
- **Cache**: a fast, derived copy that must be rebuildable from the truth. Here: Redis counters.

Ways the cache drifts: Redis restart without persistence (counters vanish, and every budget
looks empty!), a crash between reserve and settle (a stuck hold that blocks spend), a failed
settle while Redis was briefly unreachable. **Reconciliation** recomputes the counters with
`SUM(cost_usd) … GROUP BY team_id / feature` for the current day and month, and overwrites
Redis in one `MULTI/EXEC` so readers never see a half-rebuilt state.

→ `app/budgets/reconcile.py`, run in the app lifespan and via `POST /v1/budgets/reconcile`.

## 8. Fail-open vs fail-closed

When a *safety* dependency fails, you must choose:
- **Fail-open**: allow the action. Availability wins, the product keeps working, and the risk
  (unchecked spend) is temporary and visible. Example: a feature-flag service being down
  shouldn't take the site down.
- **Fail-closed**: block the action. Safety wins, and an outage of the guard becomes an outage
  of the service. Example: a payment fraud check, or a firewall.

For LLM budgets, a short fail-open window risks some overspend; fail-closed turns a Redis
blip into every AI feature failing. There's no universally right answer: it's a business
decision, so it's a config flag (`BUDGET_FAIL_MODE`). Whichever you pick, never fail
*silently*: log loudly and tell the client (`X-Budget-Warning: budget-check-unavailable`).

## 9. TTLs and time windows (UTC)

- Budgets use **calendar windows**: UTC day `[00:00, 24:00)` and UTC month. The alternative is
  *rolling* windows (the last 24 h), which are smoother but need per-request timestamps (sorted
  sets) instead of simple counters.
- **UTC** because servers and people are in different time zones, and DST creates 23h/25h
  days. UTC makes "today" and "resets at" unambiguous for everyone.
- The period key is part of the Redis key (`…:day:2026-09-30:spent`), so a new day
  automatically starts at zero. No reset job is needed.
- **TTL** (time to live) makes Redis delete old keys automatically. TTL = time until reset +
  24 h grace, so a request that reserved at 23:59:59 can still settle into *that* day's key
  after midnight.

## 10. HTTP 402 vs 429 vs 403

| Code | Meaning | Client behaviour |
|---|---|---|
| **402 Payment Required** | Out of budget/credit | Don't retry until reset or top-up |
| **429 Too Many Requests** | Rate limit, too fast | Back off and retry soon (often automatic) |
| **403 Forbidden** | Not permitted at all | Don't retry; fix permissions |

Budget exhaustion is 402: the caller is authorised and not too fast, just out of money.
Returning 429 would make SDKs retry in a loop and hammer the gateway.

## 11. Background tasks and eventual consistency

FastAPI `BackgroundTasks` run **after the response is sent**, in the same process. Moving the
audit INSERT there takes Postgres latency off the critical path.

Cost: **eventual consistency**. For a few milliseconds the response exists but the log row
doesn't, so a `/v1/usage` call made instantly may miss it (the smoke test polls for this
reason). And if the process crashes in that gap, the row is lost. Stronger options: write
inline (slower, consistent), or publish to a durable queue (Kafka/SQS/Redis Streams) and
consume it into Postgres (fast and durable, more infrastructure).

Note what is *not* eventually consistent: the Redis counters are updated inline, so
enforcement is always current.

## 12. Dependency injection

A component receives its dependencies from outside instead of constructing them.
`Gateway(registry, adapters, budgets)` doesn't know whether `budgets` talks to real Redis or
fakeredis. `create_app(settings, engine=..., redis_client=...)` is the composition root that
wires everything once at startup.

Benefits: testability (inject fakes), one shared instance of expensive resources (connection
pools), and swappability (a different store later without touching the gateway). It's the
same idea as Phase 1's adapters.

---

## Self-check questions

1. **Why can't the gateway just check `spent < limit` before the call and add the cost after?**
   Concurrent requests all read the same `spent`, all pass, and together overshoot. Reserving
   the worst case makes in-flight spend visible to everyone else.

2. **Why store nano-dollar integers in Redis instead of using `INCRBYFLOAT`?**
   Floats can't represent most decimal fractions exactly, and errors accumulate over millions of
   increments. Integer `INCRBY` is exact; 10⁻⁹ USD resolution is finer than any token price.

3. **What exactly does the Lua script prevent that `GET` then `INCRBY` doesn't?**
   A time-of-check to time-of-use race. Another request can run between the read and the write.
   A script runs atomically, so check and reserve are one step.

4. **Redis was flushed at 14:00. What happens without reconciliation, and what does it do?**
   All counters read 0, so every team gets its full budget again and overspends. Reconciliation
   recomputes today's and this month's spend from `request_logs` and rewrites the counters.

5. **When would you choose `BUDGET_FAIL_MODE=closed`?**
   When overspend is worse than downtime: hard contractual or regulatory caps, prepaid
   customer credit, or free tiers open to abuse.

6. **Why is the audit write a background task, and what can go wrong?**
   It keeps DB latency off the response path. Reads may briefly lag (eventual consistency),
   and a crash after responding can lose the row. Use inline writes or a durable queue if every
   row must survive.
