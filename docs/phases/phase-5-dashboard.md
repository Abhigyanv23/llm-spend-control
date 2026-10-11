# Phase 5: Cost Dashboard

## Goal
Give humans cost visibility: spend by team and feature, month-end projections against
budgets, the savings routing produces (gross and net of verification), and whether routing is
safe, backed by a proper analytics layer rather than ad-hoc queries in the UI.

## What was built
- Migration `0003`: analytics columns promoted from `request_logs.metadata` (`routed_tier`,
  `route_source`, `classifier_confidence`, `baseline_cost_usd`, `escalated`, `pre_escalated`,
  `downgraded`) plus `prompt_fingerprint` and an optional `prompt_preview`; backfill; three
  new/upgraded indexes (incl. a covering index)
- Gateway/audit write the new columns for every request
- `app/analytics/`: pure query functions for spend time series (zero-filled), cost by model,
  top requests and prompt patterns, projections (run-rate, trailing 7-day, EWMA) with
  burn-down and exhaustion dates, gross/net savings by feature and tier, routing quality with
  Wilson intervals, latency percentiles (Postgres `percentile_cont`, Python fallback), errors,
  and headline KPIs
- `/v1/analytics/*` API (8 endpoints) with window validation and a TTL cache
- Streamlit dashboard (`dashboard/app.py`): Overview, Spend, Budgets, Savings, Routing quality,
  Performance; reads only from the API
- `scripts/seed_demo_data.py` (deterministic, idempotent demo traffic) and
  `scripts/smoke_dashboard.py` (22 checks incl. a headless dashboard run)

## Data flow

```
gateway ──INSERT──▶ request_logs (columns + JSON) ──┐
worker  ──INSERT──▶ verifications, routing_misses ──┼─▶ app/analytics (SQL aggregates)
budgets ──INSERT──▶ budget_policies, budget_alerts ─┘        │
                                                   /v1/analytics/* (validate, cache 30 s, JSON)
                                                             │
                                               dashboard/app.py (Streamlit + Altair)
```

## Design decisions

**Columns for reads, JSON for the record.** Aggregating JSON needs a cast per row, can't use
B-tree indexes and is dialect-specific. Promoted columns are typed and indexable; the JSON stays
complete. The gateway writes both, and the migration backfills the columns for history.

**Set-based backfill, then indexes.** One `UPDATE` with JSONB operators on Postgres (a portable
Python loop elsewhere), run before index creation. Verified on real data: 0 mismatches.

**Pure analytics functions.** No HTTP in `app/analytics/`; functions return `Decimal`, `date` and
`datetime`. The API serialises (money via `usd_str`, times as UTC ISO). The maths is tested with
exact values on SQLite, and Postgres-specific paths are exercised by the smoke test.

**Three projections, shown side by side.** Run-rate is the headline (simple to explain);
trailing 7-day and EWMA are shown next to it so a reader can see when they disagree, which is
itself the signal that the run-rate is distorted by a spike or a weekday effect.

**Wilson intervals, not normal approximation.** Verification samples are small; the Wilson
interval stays honest for small n and extreme rates.

**Net savings subtracts verification.** Quality assurance is an overhead of routing; the
dashboard always shows gross and net together.

**A short TTL cache, no invalidation.** Dashboards tolerate 30 s of staleness; enforcement never
reads this cache. Responses say `cached` and `generated_at`.

**Thin UI.** Streamlit only calls the API, so business logic lives in one place and the API can
later be secured. Altair because it is declarative, ships with Streamlit, and handles time axes
and tooltips well.

**Fingerprints, not prompts.** Prompt patterns are grouped by a SHA-256 of the normalised
instruction text. Previews are stored only when privacy settings allow.

## Known limitations
- Analytics run on the OLTP database; at scale they belong on a read replica or a warehouse
  with pre-aggregated rollups (daily cost per team/feature/model)
- Old rows have no fingerprint (prompts were never stored); fingerprints are pseudonymous,
  not anonymous (an HMAC with a secret key would resist guessing)
- Projections ignore weekly seasonality and growth explicitly; month-start projections are noisy
- The cache is per process and expiry-only; several API instances may disagree for ≤ 30 s
- No auth: anyone who can reach the API can read every team's spend and prompt previews
- Demo data illustrates the UI; it is not a measurement (Phase 6 produces real numbers)
- The dashboard has no screenshots in the repo yet

## Key formulas
```
run_rate      = MTD / max(elapsed_days, 1) × days_in_month
trailing_7d   = MTD + mean(daily spend, last 7 complete days) × remaining_days
ewma          = MTD + EWMA_0.3(daily spend this month) × remaining_days
exhaustion    = now + (limit − MTD) / (MTD / elapsed_days)   if before month end
gross_savings = Σ baseline_cost − Σ cost              (routed, successful)
net_savings   = gross_savings − Σ verification_cost
wilson(p̂, n)  = (p̂ + z²/2n ± z·√(p̂(1−p̂)/n + z²/4n²)) / (1 + z²/n),   z = 1.96
percentile_cont(q) = x[⌊q(n−1)⌋] + (q(n−1) − ⌊q(n−1)⌋) · (x[⌈q(n−1)⌉] − x[⌊q(n−1)⌋])
```

## Interview talking points
- **How the projection works and its limits**: run-rate is MTD scaled to the month; it explodes on
  early spikes, so we floor elapsed time, show 7-day and EWMA alongside, and compute the
  exhaustion date from the run-rate. Seasonal models are the next step.
- **Gross vs net savings**: routing saved 53% gross on the demo data, 44% after paying for
  verification. In live Phase 4 runs, aggressive sampling made net savings negative.
- **OLTP vs OLAP on one database**: denormalised columns + covering index now; replica or warehouse
  with rollups later.
- **Bugs only Postgres found**: a `GROUP BY` on two separately built `coalesce()` expressions,
  and a `datetime` overflow from a near-zero run-rate. Both passed on SQLite; the live smoke test
  caught them.
