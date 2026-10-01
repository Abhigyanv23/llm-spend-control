# Phase 4: Complete Change Record

Every change made in Phase 4 (Quality Checks and Escalation), file by file, with the real
verification results.

| | |
|---|---|
| Release | `v0.4.0` |
| Branch | `feat/phase-4-verification` (from `main` at `v0.3.0`, merged via pull request) |
| Size | 46 files changed (28 new, 18 modified), +4,594 / −168 lines |
| Schema changes | Migration `0002`: `verifications`, `routing_misses` (upgrade + downgrade) |
| New dependencies | None (Redis Streams via the existing `redis` client; `difflib`/`hashlib` are stdlib) |
| Verification | 210/210 pytest · 19/19 quality smoke · 13/13 Phase 1–2 smoke · 13/13 routing smoke, against PostgreSQL 16 + Redis 7 (Docker) |

Related documents: [design decisions](phase-4-quality.md) · [theory notes](../notes/phase-4-theory.md) ·
[API reference](../api.md) · [architecture](../architecture.md) · [CHANGELOG](../../CHANGELOG.md)

---

## 1. Summary

Before Phase 4, the router's choice was final and unmeasured. After Phase 4:

- **A sample of cheap answers is re-checked** by a stronger model in a separate worker
  process, through a Redis Streams queue with at-least-once delivery and idempotent storage.
- **Every verdict is stored**; failures become **routing misses**, labelled training data,
  exportable as JSONL.
- **Visibly broken cheap answers are fixed synchronously** by a one-step cascade, and
  uncertain high-stakes requests start one tier higher.
- **Quality is measurable**: miss rate with a margin of error, escalation rate, verification
  spend, and savings net of quality overhead (`GET /v1/quality`).
- **Quality assurance is budgeted** like any other spend.

## 2. Behaviour changes

| Change | Before | After | Compatible? |
|---|---|---|---|
| Routed `high`/`critical` request, classifier confidence < 0.6 | Classifier tier | One tier higher (pre-call escalation) | Behaviour change (intended); `/v1/route/preview` shows it |
| Routed answer is empty / refusal / truncated / invalid JSON | Returned as-is | Retried once on the next tier; better answer returned | Behaviour change (intended) |
| `cost_usd`, `usage` of an escalated request | One call | Sum of all attempts | Yes (one audit row, same shape) |
| `metadata.escalation`, `metadata.quality` in responses and audit rows | — | Added | Yes (additive) |
| Mock output for prompts > 200 chars on `mock-medium` / `mock-large` | Echoed 200 chars | Echoes 1,000 / 4,000 chars | Dev only; short prompts byte-for-byte identical |
| Startup with `VERIFY_ENABLED=true` and no usable tier-3 model | Started | Fails with `QualityConfigError` (fail fast) | Behaviour change: set the key, use `dev`, or `VERIFY_ENABLED=false` |
| Startup reconciliation | Request spend only | Request spend + verification spend | Fix |
| New errors | — | `503 queue_unavailable` on `/v1/quality/queue` | Yes (new code) |
| `/health` | 5 fields | Unchanged | Yes |

## 3. New behaviour at a glance

| Situation | Result |
|---|---|
| Routed, tier 1–2, confident (≥ 0.6) | Sampled with probability 10% |
| Routed, tier 1–2, unsure (< 0.6) | Sampled with probability 50% |
| Explicit / pinned model, tier 3, escalated, or `VERIFY_ENABLED=false` | Never sampled (reason recorded) |
| Sampled | Job `XADD`ed after the response; enqueue failure only logged |
| Worker: cheap answer matches the reference | `verifications.verdict = pass` |
| Worker: cheap answer doesn't match | `fail` + a `routing_misses` row (prompt capped / optional) |
| Worker: judge output unusable | `inconclusive` (not counted in the miss rate) |
| Worker: verifier budget exhausted | `skipped`, acked, no model call |
| Worker: provider error | Not acked → reclaimed after `job_timeout_s` → retried → dead-letter after `max_attempts` |
| Worker: malformed job | Dead-letter immediately (poison message) |
| Same job delivered twice | One row; the second reports `duplicate` |
| Post-call check fails, escalation possible | Next tier called; summed cost; `escalated: true` |
| Escalation blocked by budget / provider error / no higher tier / explicit model | Original answer returned + `blocked` / `note` |
| Escalated answer still fails | Stops at `max_escalations: 1` (`final_check_failed` set) |

## 4. Changes by file

### 4.1 Configuration

| File | Status | What changed |
|---|---|---|
| `config/quality.yaml` | new | `sampling` (rates, threshold, skipped sources, skip top tier), `verification` (reference model `auto`, judge type/model/threshold, verifier budget, max attempts, job timeout, stream names, consumer group, `maxlen`), `escalation` (pre-call priorities + confidence, post-call checks, `max_escalations`, refusal patterns), `privacy` (`store_prompts`, `max_prompt_chars`) |
| `app/config.py` | modified | `quality_config_path`, `verify_enabled`, `worker_concurrency` (1–64), `worker_consumer_name` |
| `.env.example` | modified | Phase 4 block with the four new variables |
| `.gitignore` | modified | `*.jsonl` (exports contain prompts) |

### 4.2 Database

| File | Status | What changed |
|---|---|---|
| `migrations/versions/0002_verifications_and_routing_misses.py` | new | Revision `0002` (down `0001`). `verifications`: verdict CHECK (`pass`/`fail`/`inconclusive`/`skipped`), `NUMERIC(14,8)` cost, JSONB metadata, UNIQUE `request_id`, indexes on `(feature, created_at)`, `(model, created_at)`, `(created_at)`. `routing_misses`: UNIQUE `request_id`, nullable `prompt`, chosen/better model and tier, classifier confidence and features (JSONB), indexes on `(feature, created_at)`, `(created_at)`. Full downgrade |
| `app/db/models.py` | modified | `VERIFICATION_VERDICTS`, ORM `Verification`, `RoutingMiss` (same naming convention; `meta` ↔ `metadata`) |
| `app/db/__init__.py` | modified | Exports the new models |

### 4.3 Quality domain (`app/quality/`, all new)

| File | What it does |
|---|---|
| `config.py` | Dataclasses + `load_quality_config()` with fail-fast validation; resolves the reference model (`auto` = first usable tier-3 candidate) and the judge model |
| `sampling.py` | `hash_fraction()` (SHA-256 → [0, 1)), `decide_sampling()` → `SampleDecision` |
| `judges.py` | `Judge` interface, `SimilarityJudge` (sequence/Jaccard, mock-tag normalisation, `autojunk=False`), `LLMJudge` (rubric prompt, temperature 0, section caps, worst-case cost), `parse_judge_output()` (fenced/embedded JSON, strict validation → `inconclusive`, inconsistency flag), `build_judge()` |
| `jobs.py` | `VerificationJob`: self-contained, versioned payload; `to_fields`/`from_fields`; `prompt_text()` |
| `queue.py` | `VerificationQueue`: `enqueue` / `enqueue_safe`, `ensure_group` (BUSYGROUP-safe), `read`, `reclaim` (`XAUTOCLAIM`), `delivery_count`, `ack`, `dead_letter` (MULTI: XADD + XACK), `remove_consumer_if_idle`, `stats` |
| `store.py` | `verification_exists`, `save_verification` (verification + miss in one transaction; `IntegrityError` → duplicate) |
| `worker.py` | `VerificationWorker`: `run_once` / `run_forever`, reclaim-then-read batches, bounded concurrency, idle sleep, per-job flow (parse → attempts → idempotency → budget → reference → judge → settle → store → ack), `prompt_for_storage` |
| `escalation.py` | `apply_pre_call_escalation()`, `check_answer()` (empty, truncated `length`/`max_tokens`, refusal phrases in the first 300 chars incl. curly quotes, invalid JSON only when JSON was asked for) |
| `reports.py` | `quality_summary()` (verdicts, rates, margin, weighted miss rate, per-model, misses, sampling, escalations, costs, net savings), `list_misses()`, `iter_misses()` (keyset batches) |
| `__init__.py` | Package exports |

### 4.4 Core application

| File | Status | What changed |
|---|---|---|
| `app/bootstrap.py` | new | `build_core()`: one composition root (registry, routing + quality config, router, adapters, engine, Redis, budgets with verifier scope, queue) for the API and the worker; `Core.aclose()` closes only what it created |
| `app/worker.py` | new | `python -m app.worker [--once] [--concurrency N] [--consumer NAME]`; patient Redis client (≥ 5 s); Windows-safe stop handlers (`signal.signal` + `call_soon_threadsafe`; SIGINT/SIGTERM/SIGBREAK) |
| `app/gateway.py` | modified | Pre-call escalation after routing; post-call checks and `_post_call_escalation()` (separate reserve/settle per attempt, graceful degradation); `_Escalation` state, `_attempt_meta()`; summed cost/tokens/latency/estimate in one audit record; savings net of escalation; `_sample()` builds the `VerificationJob`; `GatewayResult.verification_job`; `metadata.escalation`, `metadata.quality` |
| `app/main.py` | modified | Lifespan uses `build_core()`; `app.state.queue`, `app.state.core`; enqueue as a background task; quality router; version `0.4.0` |
| `app/api/quality.py` | new | `GET /v1/quality`, `/v1/quality/misses`, `/v1/quality/queue` (`503 queue_unavailable`) |
| `app/api/routing.py` | modified | Preview applies pre-call escalation; returns `pre_call_escalation` |
| `app/routing/router.py` | modified | `pick_from_tier()`, `downgrade_enabled()` |
| `app/budgets/service.py` | modified | `verifier_scope` passed to reconciliation |
| `app/budgets/reconcile.py` | modified | Adds `SUM(verifications.verification_cost_usd)` to the verifier team/feature counters |
| `app/providers/mock_adapter.py` | modified | Capability limits per mock; tier-1 directives `empty`, `refuse`, `truncate` (`finish_reason: length`), `badjson`; `metadata.mock_directive` |

### 4.5 Scripts

| File | Status | What changed |
|---|---|---|
| `scripts/smoke_quality.py` | new | 19 live checks: 4 escalation triggers, max-escalations stop, budget-blocked escalation, pre-call bump (own team), one audit row per escalated request, sampling until 2 short + 2 long, `app.worker --once`, verdicts, miss rate, misses listing, escalation counts, costs/net savings, queue drained, verifier budget charged. Fresh `smoke4-<run id>` teams; `GATEWAY_URL` |
| `scripts/export_misses.py` | new | JSONL export (`--out`, `--since`, `--until`, `--feature`, `--team`) with `label_tier` |
| `scripts/seed_budgets.py` | modified | `quality-verifier` team: $1.00/day, $20.00/month |

### 4.6 Tests (117 new; 210 total)

| File | Status | Tests | Covers |
|---|---|---|---|
| `tests/test_quality_config.py` | new | 18 | Default load, reference model per profile/keys, verify disabled, LLM judge default, 13 invalid-config cases |
| `tests/test_sampling.py` | new | 11 | Hash determinism and range, per-request stability, convergence to 10% and 50% within 4 SE over 20,000 ids, threshold edge, 5 ineligibility reasons, rates 0 and 1 |
| `tests/test_mock_adapter.py` | new | 13 | Short prompts unchanged on all mocks, capability limits, 4 directives, invalid JSON, directives ignored by stronger mocks |
| `tests/test_judges.py` | new | 22 | Normalisation, mock pass, realistic truncation fail, Jaccard vs sequence, empty cases, 4 valid + 8 unusable judge outputs, inconsistency flag, LLM judge cost/request, garbage → inconclusive, provider errors propagate, worst-case cap |
| `tests/test_worker.py` | new | 17 | Pass + ack, fail + routing miss + prompt cap, privacy off, concurrent redelivery → one row, sequential redelivery → no second spend, retry → dead letter (holds released), transient → success on attempt 2, visibility timeout, poison message, budget → skipped, verifier spend charged, judge crash still settles reference cost, stats + consumer cleanup, job versioning, `run_forever` stop, reconciliation keeps verification spend |
| `tests/test_verification_api.py` | new | 4 | Routed request sampled + queued, explicit not sampled, enqueue failure doesn't fail the request, chat → worker → miss end to end |
| `tests/test_escalation.py` | new | 19 | 7 visible-failure cases, late refusal ignored, 5 JSON cases, disabled checks, pre-call bump + metadata, 2 non-applicable cases, feature max tier, explicit model |
| `tests/test_escalation_api.py` | new | 13 | Empty → escalated with summed cost, single audit row and budget counters; refuse/truncate; max escalations; budget-blocked escalation; explicit and capped features; healthy answer; pre-call via chat and preview; escalated not sampled; quality endpoints + export; time window |
| `tests/conftest.py` | modified | — | Shared `sampled_api` fixture (100% sampling, yields client + app) |

No Phase 1–3 test was changed.

### 4.7 Documentation

| File | Status | What changed |
|---|---|---|
| `README.md` | modified | Phase 4 features, stack (Streams), quickstart with the worker, quality smoke test, "Quality checks and escalation" section, 8 new troubleshooting rows, 4 new settings, layout, limitations, doc links |
| `CHANGELOG.md` | modified | New `[0.4.0]` entry |
| `docs/architecture.md` | modified | Component diagram with worker, stream and quality API; 12-step lifecycle; routing flowchart with pre-call bump; escalation-cascade flowchart; verification sequence diagram (sample → enqueue → worker → reference → judge → store → ack); Redis keys incl. streams; processes table; principles |
| `docs/api.md` | modified | `metadata.escalation`, `metadata.quality`, preview `pre_call_escalation`, `/v1/quality`, `/misses`, `/queue`, new 503 codes |
| `docs/phases/phase-4-quality.md` | new | Goal, two loops, design decisions, known limitations, formulas, interview talking points |
| `docs/notes/phase-4-theory.md` | new | 21 theory topics (4A: evaluation, sampling, judges, routing as classification, privacy; 4B: queues, delivery guarantees, Streams, DLQs, backpressure, workers, QA cost; 4C: cascades, graceful degradation, measuring safety, feedback loops) + 18 self-check questions |
| `docs/phases/phase-4-changes.md` | new | This document |

## 5. New configuration

| Variable | Default | Used by |
|---|---|---|
| `QUALITY_CONFIG_PATH` | `config/quality.yaml` | app, worker |
| `VERIFY_ENABLED` | `true` | app, worker |
| `WORKER_CONCURRENCY` | `4` | worker |
| `WORKER_CONSUMER_NAME` | `<hostname>-<pid>` | worker |

## 6. New and changed endpoints

| Method | Path | Status |
|---|---|---|
| `POST` | `/v1/chat` | changed: pre-call escalation, post-call cascade, `metadata.escalation`, `metadata.quality` |
| `POST` | `/v1/route/preview` | changed: applies pre-call escalation, returns `pre_call_escalation` |
| `GET` | `/v1/quality` | new |
| `GET` | `/v1/quality/misses` | new |
| `GET` | `/v1/quality/queue` | new |

New CLI: `python -m app.worker`, `python scripts/export_misses.py`, `python scripts/smoke_quality.py`.

## 7. Issues found and fixed during verification

| Issue | How it was found | Fix |
|---|---|---|
| Two concurrent deliveries of one job were both reported `pass` (the UNIQUE constraint correctly stored one row) | `test_redelivered_job_is_not_stored_twice` | `save_verification` returns whether it inserted; the loser reports `duplicate`. Documented that its model calls were still paid for |
| Test expected the full prompt in `routing_misses` | pytest | Test was wrong: the 2,000-char privacy cap worked as designed; assertion fixed |
| Worker loop spun at 100% CPU and starved the event loop when the stream was empty | `run_forever` test hung: fakeredis returns from `XREADGROUP BLOCK` immediately and never yields | 0.1 s idle sleep after an empty poll (also protects against any non-blocking backend) |
| A stopped continuous worker stayed listed as a consumer | `/v1/quality/queue` during the Part 4C live run | `run_forever` removes its consumer on graceful stop if nothing is pending |
| Smoke test counted the pre-call request's verification with the main team (3 FAIL) | First `smoke_quality.py` run (15/18) | Test bug: the bumped tier-2 request is legitimately sampleable; it now uses its own team |
| **Verifier budget reset to $0 on every API restart** | `smoke_quality.py` showed verifier spend equal to a single run's, not the day's total | Reconciliation now adds `verifications.verification_cost_usd` to the verifier team/feature counters. Regression test added; live check: Redis $0.155385 = Postgres $0.155385 |
| Python 3.12+ `sum()` hides float drift (Phase 2 lesson, reapplied) | — | Money stays `Decimal` / nano-dollars everywhere, including verification spend |

## 8. Verification results

| Check | Environment | Result |
|---|---|---|
| `python -m pytest` | fakeredis + SQLite | **210 passed** (93 Phase 1–3 + 117 Phase 4), ~15–25 s |
| `alembic upgrade head` → `downgrade 0001` → `upgrade head` | PostgreSQL 16 (Docker) | Clean both ways; `\dt` shows 6 tables |
| `alembic check` | SQLite | No drift between models and migrations |
| App startup | dev profile | `Quality config loaded: verification enabled (reference model: mock-large, judge: similarity)` |
| Part 4B live run: 12 requests (6 short, 6 long), `app.worker --once` | Postgres + Redis | 8 sampled → **4 pass (short), 4 fail (long)**, 4 routing misses, 0 errors, pending 0 |
| Part 4B checkpoint: 2 × 12 requests, `--once` then continuous worker | Postgres + Redis | 9 sampled → 9 verified (4 pass, 5 fail), pending 0, dead-letter 0; continuous worker picked up batch 2 |
| Part 4C live run: 4 directives + high-priority request | Postgres + Redis | All 4 escalated to `mock-medium`; `badjson` stopped at `max_escalations`; pre-call bump to `mock-medium` |
| `GET /v1/quality` (all data, Part 4C) | Postgres (JSONB queries) | 17 verified, miss rate **52.9% ± 23.7%**, 9 routing misses |
| `scripts/export_misses.py` | Postgres | 9 misses exported as JSONL; file ignored by git |
| `scripts/smoke_quality.py` | Postgres + Redis | **19/19** |
| `scripts/smoke_test.py` | Postgres + Redis | 13/13 (Phase 1–2 unchanged) |
| `scripts/smoke_routing.py` | Postgres + Redis | 13/13 (Phase 3 unchanged) |
| Reconciliation after restart | Postgres + Redis | Verifier spend in Redis $0.155385 = `SUM(verification_cost_usd)` $0.155385 |

**Measured cost of quality assurance** (dev profile, mock prices mirroring real tiers):

| Run | Requests cost | Verification cost | Overhead | Net savings |
|---|---|---|---|---|
| Part 4B live (8 of 12 sampled) | $0.00055 | $0.0476 | ~87× | negative |
| `smoke_quality.py` (team) | $0.00106 | $0.0240 | 2,266% | −$0.0126 |

Verifying long prompts with a tier-3 reference at a 50% sample rate costs far more than the
cheap requests themselves. Sample rates and the verifier budget are therefore cost controls,
not just quality knobs.

## 9. Known limitations carried forward

Similarity judge suits mocks only; LLM-judge biases need human spot-checks · post-call checks
catch visible failures only · escalation steps one tier at a time · a racing duplicate pays for
a second reference call · spend of jobs that dead-letter after the reference call isn't
restorable by reconciliation · verification spend is reconciled to the current verifier team ·
`/v1/quality` aggregates JSON metadata on every call · prompts sit in Redis until trimmed ·
no alerting on miss/escalation/dead-letter rates · no authentication. Plus everything listed
for Phases 2 and 3. Details in [phase-4-quality.md](phase-4-quality.md#known-limitations).
