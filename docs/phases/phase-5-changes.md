# Phase 5: Complete Change Record

Every change made in Phase 5 (Cost Dashboard), file by file, with the real verification results.

| | |
|---|---|
| Release | `v0.5.0` |
| Branch | `feat/phase-5-dashboard` (from `main` at `v0.4.0`, merged via pull request) |
| Size | 33 files changed (20 new, 13 modified), about +3,100 / −25 lines |
| Schema changes | Migration `0003`: 9 columns on `request_logs` + backfill; 2 new indexes; team index made covering |
| New dependencies | `streamlit`, `altair` (dashboard section of `requirements.txt`) |
| Verification | 254/254 pytest · 22/22 dashboard smoke (incl. headless render) · 13/13 + 13/13 + 19/19 earlier smoke tests, against PostgreSQL 16 + Redis 7 |

Related documents: [design decisions](phase-5-dashboard.md) · [theory notes](../notes/phase-5-theory.md) ·
[API reference](../api.md) · [architecture](../architecture.md) · [CHANGELOG](../../CHANGELOG.md)

---

## 1. Summary

Before Phase 5, cost and quality data existed but could only be read through raw endpoints and
SQL. After Phase 5:

- **Analytics-friendly schema**: hot fields promoted from JSON to indexed columns, history
  backfilled, prompt fingerprints for every new request.
- **An analytics layer** of pure, tested query functions: spend series, projections,
  burn-down, gross/net savings, routing quality with Wilson intervals, latency percentiles,
  errors, KPIs.
- **A read-only analytics API** with validation and a TTL cache.
- **A Streamlit dashboard** that reads only from that API.
- **Demo data** so every chart can be evaluated, and a smoke test that renders the dashboard.

## 2. Behaviour changes

| Change | Before | After | Compatible? |
|---|---|---|---|
| `request_logs` | Analytics fields only in `metadata` | Also in typed columns (written by the gateway, backfilled by `0003`) | Yes (additive; JSON unchanged) |
| Every request | — | Gets a `prompt_fingerprint`; a 120-char `prompt_preview` when `privacy.store_prompts` is on | Yes (additive) |
| `config/quality.yaml` | — | `privacy.prompt_preview_chars` (default 120, 0 = none) | Yes |
| New endpoints | — | `/v1/analytics/*` (8) | Yes |
| New error | — | `400 invalid_window` | Yes (new code) |
| `/health`, `/v1/chat` response | — | Unchanged | Yes |

## 3. Changes by file

### 3.1 Schema and data

| File | Status | What changed |
|---|---|---|
| `migrations/versions/0003_analytics_columns.py` | new | Adds `routed_tier`, `route_source`, `classifier_confidence`, `baseline_cost_usd`, `escalated`, `pre_escalated`, `downgraded`, `prompt_fingerprint`, `prompt_preview` (batch ALTER for SQLite); backfill: one set-based `UPDATE` with JSONB operators on Postgres, a portable Python loop elsewhere; then indexes `(model, created_at)`, `(prompt_fingerprint, created_at)`, and `(team_id, created_at) INCLUDE (cost_usd)`. Full downgrade |
| `app/db/models.py` | modified | The 9 columns and 2 indexes on `RequestLog`; team index with `postgresql_include` |
| `app/fingerprint.py` | new | `instruction_text`, `normalise` (lower-case, digits → 0, whitespace), `fingerprint_text` (SHA-256), `prompt_fingerprint`, `prompt_preview` |
| `app/audit.py` | modified | `AuditRecord` gains the analytics fields; `AuditLogger.write` stores them |
| `app/gateway.py` | modified | Fills fingerprint/preview at the start, route source and confidence after routing, `pre_escalated`, `baseline_cost_usd`, final `routed_tier`, `escalated`, `downgraded`; error paths set tier/downgrade where known; `_preview()` respects privacy |
| `app/quality/config.py`, `config/quality.yaml` | modified | `privacy.prompt_preview_chars` (validated ≥ 0) |

### 3.2 Analytics layer (`app/analytics/`, all new)

| File | What it does |
|---|---|
| `common.py` | `AnalyticsFilter` (window + team/feature, `conditions()`, `days()`), `make_filter()` (defaults, `WindowError`), UTC `day_bucket()` per dialect, `wilson_interval()`, `percentile_cont()` (Postgres-identical interpolation), money/ratio helpers |
| `spend.py` | `spend_timeseries()` (zero-filled, by team/feature/model), `cost_by_model()`, `top_requests()`, `top_patterns()` (by fingerprint, with models used), `error_breakdown()` |
| `projections.py` | `ewma()`, `project()` (run-rate, trailing 7-day, EWMA), `exhaustion()` (status + date, overflow-safe), `projections()` per team/feature with limits and burn-down |
| `quality.py` | `savings()` (gross, overhead, net, by feature/tier), `routing_quality()` (tiers, sources, escalations, downgrades/blocks/overrides, verdicts with Wilson CI, per model, misses), `latency_by_model()` (Postgres `percentile_cont`, Python fallback) |
| `summary.py` | KPIs: window totals and error rate, today/MTD spend, projected month end, savings %, pass rate + CI, escalation rate, scopes at risk, active alerts |
| `cache.py` | `TTLCache` (expiry + LRU, `get_or_compute`, TTL 0 disables) |
| `__init__.py` | Package exports |

### 3.3 API and app

| File | Status | What changed |
|---|---|---|
| `app/api/analytics.py` | new | 8 endpoints; `window` dependency (`400 invalid_window`); `respond()` with cache key = endpoint + filter + params, `generated_at`, `cached`; `jsonable()` (Decimal → fixed-point string, datetimes → UTC ISO) |
| `app/main.py` | modified | Analytics router, `app.state.analytics_cache`, version `0.5.0` |
| `app/config.py` | modified | `analytics_cache_ttl_s` (30), `analytics_max_window_days` (366) |
| `.env.example` | modified | Phase 5 block incl. `GATEWAY_URL` |
| `requirements.txt` | modified | Dashboard section: `streamlit`, `altair` |

### 3.4 Dashboard and scripts

| File | Status | What changed |
|---|---|---|
| `dashboard/app.py` | new | Streamlit app: sidebar window/team/feature + refresh; tabs Overview, Spend, Budgets (projections + burn-down), Savings, Routing quality (Wilson error bars), Performance; `st.cache_data(ttl=30)`; empty states; "data as of"; reads only `/v1/analytics/*` |
| `scripts/seed_demo_data.py` | new | 45 days × 4 `demo-*` teams; weekday pattern, incident spike, growth trend over budget, tier mix with classifier errors, escalations, downgrades, blocks, errors, latency tail, verifications/misses, budget alerts; deterministic, idempotent, `--reset` |
| `scripts/smoke_dashboard.py` | new | 22 checks on every analytics endpoint + `AppTest` headless render (`--no-ui` to skip) |

### 3.5 Tests (44 new; 254 total)

| File | Status | Tests | Covers |
|---|---|---|---|
| `tests/test_analytics_columns.py` | new | 10 | Fingerprint grouping/normalisation, last-user-message rule, preview cap; gateway writes every column (routed, escalated, pre-call, downgraded, failed, invalid); privacy switch; backfill extraction incl. null metadata |
| `tests/test_analytics.py` | new | 16 | Wilson, percentile_cont, EWMA, window validation, zero-filled spend, cost by model, top patterns/requests, errors, gross/net savings, routing quality, latency fallback, projection formulas and statuses (incl. overflow regression), end-to-end projections + burn-down, summary, TTL cache |
| `tests/test_analytics_api.py` | new | 17 | Every endpoint (8), string money, zero-filling, savings/quality/top via real chat traffic, filters, projections status, 4 validation cases, limit bound, caching |
| `tests/test_seed_demo_data.py` | new | 1 | Deterministic, idempotent, reset removes only demo data |

No earlier test was changed.

### 3.6 Documentation

| File | Status | What changed |
|---|---|---|
| `README.md` | modified | Phase 5 features, stack, quickstart with dashboard, smoke test, "Cost dashboard" section (tabs, screenshot placeholder), 6 troubleshooting rows, 3 settings, layout, limitations, links |
| `CHANGELOG.md` | modified | `[0.5.0]` entry |
| `docs/architecture.md` | modified | Dashboard + analytics in the component diagram; analytics data-flow diagram and notes; processes table |
| `docs/api.md` | modified | All 8 analytics endpoints with real example responses and field tables |
| `docs/phases/phase-5-dashboard.md` | new | Goal, data flow, design decisions, limitations, formulas, talking points |
| `docs/notes/phase-5-theory.md` | new | 14 theory topics + 6 self-check questions |
| `docs/phases/phase-5-changes.md` | new | This document |

## 4. New configuration

| Variable | Default | Used by |
|---|---|---|
| `ANALYTICS_CACHE_TTL_S` | `30` | API |
| `ANALYTICS_MAX_WINDOW_DAYS` | `366` | API |
| `GATEWAY_URL` | `http://127.0.0.1:8000` | dashboard, smoke scripts |
| `privacy.prompt_preview_chars` (quality.yaml) | `120` | gateway |

## 5. New endpoints

| Method | Path |
|---|---|
| `GET` | `/v1/analytics/summary` |
| `GET` | `/v1/analytics/spend` (`group_by=team|feature|model`) |
| `GET` | `/v1/analytics/projections` |
| `GET` | `/v1/analytics/top` (`kind=patterns|requests`, `limit`) |
| `GET` | `/v1/analytics/savings` |
| `GET` | `/v1/analytics/quality` |
| `GET` | `/v1/analytics/latency` |
| `GET` | `/v1/analytics/errors` |

## 6. Issues found and fixed during verification

| Issue | How it was found | Fix |
|---|---|---|
| `/v1/analytics/errors` failed on Postgres: `column "request_logs.provider" must appear in the GROUP BY clause` | Live run against Postgres (passed on SQLite) | Two separately built `coalesce(provider, 'none')` expressions bind two parameters; build the expression once and reuse it |
| `/summary` and `/projections` crashed: `OverflowError: date value out of range` | Live run: a team with a $100 limit and near-zero spend projected exhaustion ~10,000 years ahead | Compare days-to-exhaustion with days left in the month before building a datetime; regression test |
| Projections filtered by team still listed every feature | `test_filters_and_projections` | Filtering one scope returns only that scope |
| Prompt preview kept a trailing space after truncation | `test_fingerprint_is_a_sha256_hex_and_preview_is_capped` | `rstrip()` after cutting |
| Streamlit deprecation: `use_container_width` | Warnings during the headless smoke run | `width="stretch"` |

## 7. Verification results

| Check | Environment | Result |
|---|---|---|
| `python -m pytest` | fakeredis + SQLite | **254 passed** (210 earlier + 44 new) |
| `alembic upgrade head` (0002 → 0003) | PostgreSQL 16 | Applied; 118 existing rows backfilled: 99 with tier, 103 with source, 98 with baseline, 24 escalated, 7 pre-call, 2 downgraded; **0 mismatches** vs JSON |
| `alembic downgrade 0002` → `upgrade head` | PostgreSQL 16 | 25 → 16 → 25 columns; backfill re-ran |
| Team index | PostgreSQL 16 | `btree (team_id, created_at) INCLUDE (cost_usd)` |
| `alembic upgrade/downgrade/check` | SQLite | Clean both ways, no drift |
| `scripts/seed_demo_data.py` | PostgreSQL 16 | 8,815 demo requests (search 3,352 · support 2,729 · research 1,169 · marketing 1,565), 806 verifications, 105 misses |
| All 8 `/v1/analytics/*` endpoints | PostgreSQL 16 | 200 (after the 2 fixes above) |
| `scripts/smoke_dashboard.py` | Postgres + Redis + Streamlit `AppTest` | **22/22**; dashboard: 0 exceptions, 16 KPI tiles, 6 tabs |
| `smoke_test.py` / `smoke_routing.py` / `smoke_quality.py` | Postgres + Redis | 13/13 · 13/13 · 19/19 (no regressions) |

**Demo-data snapshot** (illustrative, generated; not a measurement):

| Metric | Value |
|---|---|
| Gross savings vs all-strongest baseline | 53.02% ($23.55 of $44.43) |
| Verification overhead | $4.05 |
| Net savings | 43.90% |
| Verifier pass rate | 84.0% (Wilson 95%: 80.9%–86.8%), 589 judged |
| `demo-marketing` projection | 125% of its monthly limit, status `at_risk`, exhaustion ~25 Oct |
| Latency `mock-large` | p50 2.6 s · p95 5.4 s · p99 14.1 s |

## 8. Known limitations carried forward

Analytics on the OLTP database (replica/warehouse + rollups at scale) · old rows have no
fingerprint · fingerprints are pseudonymous, not anonymous · projections ignore explicit
seasonality and trend · per-process, expiry-only cache · no auth on analytics (prompt previews
included) · no dashboard screenshots in the repo yet. Plus everything listed for Phases 2–4.
Details in [phase-5-dashboard.md](phase-5-dashboard.md#known-limitations).
