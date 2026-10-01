# Phase 5 Theory Notes: Cost Dashboard

Study notes for Phase 5. Each topic: the idea, why it matters, and where it shows up in the code.

---

## Part 5A: data model for analytics

### 1. OLTP vs OLAP

- **OLTP** (online transaction processing): many small reads/writes of *individual rows*,
  low latency, strict consistency. The gateway's request path is OLTP: insert one audit row,
  look up one budget policy.
- **OLAP** (online analytical processing): few large queries that *scan and aggregate* many
  rows ("cost per team per day for 30 days"). The dashboard is OLAP.

They want different things: OLTP wants narrow indexes and few of them (every index slows every
insert); OLAP wants data laid out for scanning, often in a separate store (a column store or a
warehouse). At this project's scale one Postgres does both, but the design keeps the boundary
clear: analytics is a separate, read-only layer (`app/analytics/`), so it could later point at
a read replica or a warehouse without touching the gateway.

### 2. Denormalisation: promoting JSON fields to columns

Phases 3–4 stored routing and escalation details in `request_logs.metadata` (JSONB): flexible,
no migration per new field. For aggregation that's costly:
- every row needs a JSON parse + cast (`(metadata->'routing'->>'final_tier')::int`);
- plain B-tree indexes can't be used on JSON paths (only expression or GIN indexes);
- the syntax is dialect-specific (Postgres `->>` vs SQLite `json_extract`).

**Denormalisation** copies the hot fields into real columns (`routed_tier`, `route_source`,
`classifier_confidence`, `baseline_cost_usd`, `escalated`, `pre_escalated`, `downgraded`). The
JSON remains the complete record; the columns are a fast, typed, portable copy *for reads*.
The cost: the same fact is stored twice, so the writer (the gateway) must fill both, and they
can drift if someone updates one without the other.

### 3. Backfills

A new column is empty for existing rows. A **backfill** fills it from data you already have, here
from the JSON, in migration `0003`:
- **Set-based** on Postgres: one `UPDATE ... SET col = (metadata->...)::type`, not a row-by-row
  loop (orders of magnitude faster).
- **Before creating indexes**: otherwise every updated row also updates every new index.
- **Risks**: long-running updates lock rows and bloat the table on big datasets (batch it, or run
  it outside the deploy); a bad cast fails the whole migration (all-or-nothing is good here:
  Postgres runs DDL in a transaction); some values can't be backfilled at all. Old rows get **no
  prompt fingerprint**, because the prompt was never stored. Analytics must tolerate the gap.
- **Verify**: we checked 118 real rows: 0 mismatches between columns and JSON.

### 4. Indexes for aggregation (composite and covering)

- **Composite** `(team_id, created_at)`: equality column first, range column second. Serves
  "team X between dates A and B" with one contiguous index range scan.
- **Covering** `(team_id, created_at) INCLUDE (cost_usd)` (Postgres): the index also carries
  `cost_usd`, so "spend by team over a window" is answered from the index alone, an
  *index-only scan* that never touches the table.
- New: `(model, created_at)` for cost and latency per model; `(prompt_fingerprint, created_at)`
  for drilling into one prompt pattern.
- Every index costs writes and disk. We added three, not one per chart.

### 5. Privacy: fingerprints instead of prompts

"Which prompts cost the most?" needs to group requests by prompt, without storing prompts.
A **fingerprint** = SHA-256 of the normalised instruction text (lower-case, digits replaced,
whitespace collapsed): "Summarise ticket #4521" and "summarise ticket #87" share one fingerprint.
It's one-way (you can't recover the text), deterministic (the same pattern always groups
together) and fixed-size. A short preview is stored only if `privacy.store_prompts` allows it.
Caveat: a hash of a short, guessable prompt can be brute-forced (hash "hello" and compare), so a
fingerprint is *pseudonymous*, not anonymous. A keyed hash (HMAC with a secret) closes that gap.

---

## Part 5B: the analytics layer

### 6. Time-series bucketing, zero-filling and UTC

- **Bucketing**: `GROUP BY` the UTC calendar day. On Postgres, `date(timezone('UTC', created_at))`.
  `created_at` is `timestamptz`; converting to UTC *before* taking the date makes the bucket
  independent of the database server's timezone.
- **Zero-filling**: `GROUP BY` returns only days that had rows. Plot that and a line chart draws a
  straight line across empty days, which looks like steady spend that never happened. So the
  analytics layer generates every day in the window and fills gaps with 0.
- **UTC storage, local display**: store and compute in UTC (unambiguous, no DST gaps); convert only
  at the edge for humans. The dashboard labels everything "UTC" rather than converting
  silently. Budget periods are UTC days/months too (Phase 2), so charts and budgets agree.

### 7. Forecasting month-end spend

| Method | Formula | Strength | Weakness |
|---|---|---|---|
| Run-rate | MTD / elapsed days × days in month | Simple, explainable | A day-2 spike gets multiplied by ~15; month-start projections are noise |
| Trailing 7-day | MTD + avg(last 7 complete days) × remaining days | Reacts to recent change; a full week averages out weekday seasonality | Ignores everything older; one bad week dominates |
| EWMA (α = 0.3) | MTD + EWMA(daily spend) × remaining days | Recent days weigh most, history still counts | Still no explicit weekly pattern or trend |

Guardrails used: elapsed days floored at 1 (no month projected from 3 hours); the *exhaustion
date* is computed by comparing days, not by building a date (a tiny run-rate against a big limit
means "in 10,000 years", which overflows `datetime`, a real bug found on Postgres data).
Seasonal models (Holt-Winters, Prophet) add explicit weekday and trend components; they need
weeks of history and are harder to explain to a budget owner.

### 8. Percentiles and tail latency

An average hides the tail: 99 requests at 0.5 s and one at 30 s average 0.8 s ("fine") while one
user waited 30 s. **Percentiles** describe the distribution: p50 (the typical request), p95 and
p99 (what the unluckiest 5% / 1% experience). For LLM calls the tail is long (cold starts,
retries, very long outputs), so p95/p99 drive timeout and SLA decisions.
`percentile_cont` interpolates linearly between the two nearest values; the Python fallback for
SQLite uses the identical formula so tests and production agree.

### 9. Confidence intervals for rates: Wilson vs normal approximation

The normal approximation `p ± 1.96·√(p(1−p)/n)` (Phase 4's margin) breaks for small n or p near 0
or 1: with 2 passes out of 2 it gives 100% ± 0, "certainly perfect". It can also produce bounds
below 0 or above 1. The **Wilson score interval** fixes both: it's never zero-width, never leaves
[0, 1], and is accurate for small samples. 8/10 passes → Wilson (49%, 94%). Our real data:
19/34 → 56% (39%, 71%): honest about how little 34 verifications tell you.

### 10. Gross vs net savings

```
gross savings = Σ baseline_cost − Σ actual_cost        (routed, successful requests)
net savings   = gross − verification spend
```
Verification exists *because* we route cheaply: it's an overhead of the routing strategy, so an
honest savings number subtracts it. Escalation cost is already inside `actual_cost` (escalated
requests store the sum of all attempts). In the demo data: 53% gross, 44% net. In the Phase 4 live
runs with a 50% sample rate on long prompts, net savings were *negative*. The difference between
the two numbers is the price of knowing your routing is safe.

### 11. Caching and staleness

Dashboards issue the same aggregate queries repeatedly. A 30-second **TTL cache** keyed by
endpoint + parameters turns N viewers into one query per 30 s. Trade-offs:
- **Staleness**: new spend appears after ≤ TTL seconds. Fine for trends; not fine for
  enforcement (which is why budgets use Redis counters, never this cache).
- **No invalidation**: we don't purge on writes (that would couple the gateway to the cache);
  expiry is the invalidation. "There are only two hard things in computer science: cache
  invalidation and naming things."
- **Per-process**: each API process has its own cache; a shared Redis cache would make all
  instances agree. Responses carry `generated_at` and `cached` so the UI can show "data as of".

---

## Part 5C: the dashboard

### 12. A thin UI over an API

The Streamlit app reads **only** from `/v1/analytics/*`, never the database:
- one source of business logic (what "net savings" means is defined once, in the API);
- the API can be secured, rate-limited and versioned; the database can't safely face a UI;
- the UI is replaceable (Grafana, React, a notebook) without touching the maths.

### 13. Dashboard design

- **KPIs first**: the overview answers "are we OK?" in five seconds (spend today/MTD, projected
  month end, net savings, pass rate, escalation rate, alerts); detail lives in tabs (drill-down).
- **Units and labels everywhere**: "Cost (USD)", "Day (UTC)", "ms"; money shown with enough
  decimals to be meaningful for tiny costs.
- **Honest charts**: zero-filled series; uncertainty shown (pass rate with its Wilson interval
  as an error bar); savings shown both gross and net; empty states instead of empty axes.
- **Freshness**: a visible "data as of" timestamp and a refresh button.

### 14. Demo data

A dashboard without data can't be evaluated. `scripts/seed_demo_data.py` generates 45 days of
plausible traffic: weekday/weekend volume, an incident spike, a team on a growth trend that
outruns its budget, a tier mix with deliberate classifier errors (under-routed requests fail
verification more often), escalations, downgrades, blocks, errors and a latency tail. It is
**deterministic** (seeded), **idempotent** (replaces its own rows) and **scoped** (only `demo-*`
teams). Being honest about it matters: demo numbers illustrate the UI, they are not results.

---

## Self-check questions

1. **Why copy `routed_tier` out of the JSON when the JSON already has it?**
   Aggregations over JSON need a parse and cast per row, can't use plain B-tree indexes, and are
   dialect-specific. A typed column is fast, indexable and portable; the JSON stays as the full
   record.

2. **What does a covering index buy you, and what does it cost?**
   An index-only scan: the query is answered from the index without visiting table rows. It costs
   extra disk and slower writes, because every insert maintains the extra column in the index.

3. **Why zero-fill a daily time series?**
   `GROUP BY` omits days with no rows; a line chart then interpolates across them, showing spend
   that never happened.

4. **On day 2, team X spent $5 because of a one-off batch job. What do the three projections
   say, and which do you trust?**
   Run-rate ≈ $5/2 × 30 = $75 (inflated by the spike); trailing 7-day and EWMA dampen it but have
   little history. Trust none of them blindly early in the month: look at the burn-down and
   wait for more days.

5. **Why use a Wilson interval instead of p ± 1.96·√(p(1−p)/n)?**
   The normal approximation fails for small n and extreme p (zero-width at 0% or 100%,
   bounds outside [0, 1]); Wilson stays within [0, 1] and is accurate for small samples.

6. **Why is verification spend subtracted from routing savings?**
   It's a cost you only pay because you route to cheaper models and need to check them. Net
   savings is what routing really saves once its quality assurance is paid for.
