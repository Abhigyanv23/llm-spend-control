# Phase 6: Complete Change Record

Every change made in Phase 6 (Simulated Workload, Case Study and Portfolio Polish), with the real
results.

| | |
|---|---|
| Release | `v1.0.0` |
| Branch | `feat/phase-6-simulation` (from `feat/phase-5-dashboard`; merge Phase 5 first) |
| Size | 98 files changed vs Phase 5 (50 new, 48 modified), about +6,700 / −420 lines, incl. lint fixes and committed reports |
| Schema changes | None (migration head stays `0003`) |
| New dependencies | `matplotlib` (reports), `ruff` (lint) |
| Verification | 275/275 pytest on Python 3.13 **and** 3.11 · ruff clean · 13/13 + 13/13 + 19/19 + 22/22 smoke against the **containerized** stack · final simulations at commit `3701c2d` |

Related documents: [design](phase-6-simulation.md) · [theory notes](../notes/phase-6-theory.md) ·
[case study](../case-study.md) · [interview notes](../interview-notes.md) ·
[report](../../reports/final-v2/report.md) · [CHANGELOG](../../CHANGELOG.md)

---

## 1. Summary

- A **labelled 1,000-prompt workload** with ground-truth tiers and a leak-free held-out split.
- A **simulation harness** running three modes on the same prompts with isolated, pinned configs.
- A **report builder** with paired savings, routing-error analysis against ground truth and
  confidence intervals, regenerable from the results file alone.
- An **evidence-driven classifier upgrade** (`rules-v2`), measured once on held-out data.
- **Containers, a dev script, CI and lint**.
- **Case study, interview notes, README overhaul, release notes.**
- **Four real bugs** found by checking the numbers (section 6).

## 2. Behaviour changes

| Change | Before | After |
|---|---|---|
| Default classifier | `rules-v1` | `rules-v2` (`classifier.version` in `routing.yaml`) |
| Reconciliation of verification spend | All verification spend charged to the configured verifier team | Charged to the budget recorded on each verification; unrecorded rows not guessed |
| Phase 1 smoke cases | Team `search`, feature `summarize` | Fresh per-run team and feature |
| Demo data features | `summarize`, `chat-assistant` | `summaries`, `assistant` (no seeded feature budgets) |
| `docker-compose.yml` | Postgres + Redis | Same by default; `--profile full` adds migrate, gateway, worker, dashboard |

## 3. Changes by file

### 3.1 Workload and evaluation

| File | Status | What changed |
|---|---|---|
| `scripts/generate_workload.py` | new | 18 categories (incl. hard-no-keywords, easy-with-heavy-words, negation, simple code, JSON output, long input, multi-turn, simulated failures); slot filling; per-template train/test split (~30% held out per category); deterministic; distribution printout |
| `data/workload.jsonl` | new | 1,000 prompts: tier 1/2/3 = 480/270/250; train 699 / test 301; 220 tricky, 30 simulated failures; sha256 `6f6c16ca3da08458…` |
| `scripts/evaluate_classifier.py` | new | Offline evaluation per split, confusion matrix, misses grouped by pattern, `--compare` v1 vs v2, JSON output |
| `app/routing/classifier.py` | modified | `rules-v2`: `task_part()` (payload exclusion), `_matches_v2()` (negation), `REASONING_QUESTION`; version from config; classification reports its version |
| `app/routing/config.py`, `config/routing.yaml` | modified | `classifier.version` (validated: `rules-v1` / `rules-v2`), default `rules-v2` |

### 3.2 Simulation and report

| File | Status | What changed |
|---|---|---|
| `scripts/run_simulation.py` | new | Modes A/B/C, `--sweep`, `--classifier`, `--split`, `--limit`, `--real` (capped budgets); `ModeServer` per mode; `TokenBucket`; jittered backoff; per-mode `quality.yaml` and pinned `routing.yaml` in the run folder; per-mode team ids, private verification stream; interfering-policy check; run-id reuse guard; worker drain + verdict collection; gzip results; `run.json` with commit and dataset hash |
| `scripts/build_report.py` | new | Cost summary, paired savings, cost by tier, confusion, under/over-routing and adequacy, visible failures by check, escalation rescue, verification with Wilson CI and miss categories, sample-rate sweep, budgets, latency, charts, markdown + `summary.json` |
| `reports/final-v1/`, `reports/final-v2/` | new | Final runs: report, summary, charts, pinned configs, compressed results |
| `reports/classifier-heldout-test.json`, `reports/classifier-train.json` | new | Offline classifier evaluations |

### 3.3 Fixes in the application

| File | What changed |
|---|---|
| `app/quality/worker.py` | Verifications record `metadata.budget` (the team/feature charged) |
| `app/budgets/reconcile.py`, `app/budgets/service.py`, `app/bootstrap.py` | Reconcile verification spend by recorded budget; `verifier_scope` removed |
| `scripts/seed_demo_data.py` | Demo verifier budget recorded; demo feature names renamed |
| `scripts/smoke_test.py` | Phase 1 cases use fresh per-run ids |

### 3.4 Operations

| File | Status | What changed |
|---|---|---|
| `Dockerfile` | new | `python:3.13-slim`, dependency layer cached, non-root user |
| `.dockerignore` | new | Excludes venv, secrets, data, reports, tests, docs |
| `docker-compose.yml` | modified | `x-app` anchor; services `migrate` (one-shot), `gateway` (healthcheck), `worker`, `dashboard` under profile `full`; service-name URLs; configurable ports |
| `scripts/dev.ps1` | new | setup, up, down, migrate, seed, serve, worker, dashboard, test, smoke, simulate, report, full |
| `.github/workflows/ci.yml` | new | lint (ruff); test matrix 3.11 + 3.13; smoke job with Postgres 16 + Redis 7 service containers |
| `pyproject.toml` | new | ruff: line length 110, target 3.11, rules E/F/W/I/B/UP/SIM/ASYNC, documented ignores |
| `requirements.txt`, `.gitignore` | modified | `matplotlib`, `ruff`; ignore run logs, uncompressed results, ruff cache |
| many files | modified | Lint fixes: import order, `zip(strict=True)`, `raise ... from None`, `contextlib.suppress`, `asyncio.to_thread` for a blocking subprocess, final newlines |

### 3.5 Tests (21 new; 275 total)

| File | Tests | Covers |
|---|---|---|
| `tests/test_workload.py` | 7 | Determinism and committed-file match, size and tier mix, split by template without leakage, stable split, valid requests, directives only in their category, long inputs over the threshold, no seeded feature-budget names |
| `tests/test_report.py` | 5 | Paired savings, cost summary, confusion and routing errors incl. adequacy, Wilson CI, escalation rescue, rendering |
| `tests/test_classifier_v2.py` | 9 | Negation, payload exclusion, label edge case, reasoning questions, unchanged ordinary prompts, risk still scans payload, version reporting and validation |
| `tests/test_worker.py` | (modified) | Reconciliation by recorded budget; other verifier teams and unrecorded rows not misattributed |

### 3.6 Documentation

| File | Status |
|---|---|
| `README.md` | Rewritten: pitch, headline, CI badge, architecture, quickstart, features by phase, results, commands, configuration, troubleshooting |
| `CHANGELOG.md` | `[1.0.0]` |
| `docs/architecture.md` | Deployment and simulation flow diagrams; post-1.0 roadmap |
| `docs/case-study.md`, `docs/interview-notes.md`, `docs/release-notes-v1.0.0.md` | new |
| `docs/phases/phase-6-simulation.md`, `docs/notes/phase-6-theory.md`, `docs/phases/phase-6-changes.md` | new |

## 4. Results

Final runs at commit `3701c2d`, dataset `6f6c16ca3da08458`, dev profile, 1,000 prompts,
concurrency 16, 100 req/s, tight marketing budget $0.03/day.

| Mode (`final-v2`) | OK | Blocked | Request cost | Verification | Per 1k | Gross (paired) | Net (paired) | Served ≥ tier |
|---|---|---|---|---|---|---|---|---|
| A all strongest | 975 | 25 | $1.0208 | – | $1.02 | – | – | 100% |
| B routing only | 1000 | 0 | $0.3604 | – | $0.36 | 65.47% | 65.47% | 97.7% |
| C full, 10% | 1000 | 0 | $0.3783 | $0.2417 | $0.62 | 63.71% | **40.30%** | **98.0%** |
| C 5% | | | | $0.0911 | $0.47 | 63.71% | 55.09% | 98.0% |
| C 25% | | | | $0.3700 | $0.75 | 63.71% | 27.96% | 98.0% |
| C 50% | | | | $0.5337 | $0.91 | 63.71% | 12.29% | 98.0% |

| | rules-v1 (`final-v1`) | rules-v2 (`final-v2`) |
|---|---|---|
| Held-out accuracy (301) | 83.4% | **92.4%** |
| Held-out under / over-routed | 23 / 27 | **6** / 17 |
| Mode C gross / net savings | 61.74% / 39.54% | 63.71% / 40.30% |
| Mode C served ≥ required tier | 94.6% | 98.0% |
| Verification pass rate (C, 10%) | 97.0% (92.5–98.8), n 133 | 97.4% (93.4–99.0), n 152 |

Other findings: the cascade rescued **23/23** genuine visible failures (8 empty, 8 refusals, 6–7
truncated); 47 `invalid_json` remain (mock artifact). All verification misses were long documents;
the judge failed 0% of under-routed answers (it can't see them with mocks). Under the baseline the
marketing budget blocked 25 of 50 requests; with routing it was never approached. Client latency
p50/p95/p99: A 369/508/594 ms, B 362/480/579 ms, C 396/621/837 ms.

**Variance:** earlier exploratory runs of the same configuration gave 43–53% net savings at a 10%
sample rate; which long documents are sampled is random and dominates verification cost.

## 5. Verification results

| Check | Result |
|---|---|
| `python -m pytest` (Python 3.13) | **275 passed** |
| `python -m pytest` (Python 3.11, fresh venv) | **275 passed** |
| `python -m ruff check .` | All checks passed |
| `docker compose config` / `--profile full config` | Valid; default = postgres, redis; full adds migrate, gateway, worker, dashboard |
| `docker compose --profile full up -d --build` | Image built; migrate exited 0; gateway healthy; worker listening; dashboard `/_stcore/health` ok |
| Smoke tests against the containerized gateway | 13/13 · 13/13 · 19/19 · 22/22 (incl. headless dashboard) |
| `scripts/dev.ps1 test` | ruff clean, 275 passed |
| Generator determinism | Two runs, identical SHA-256 |
| Reconciliation after the attribution fix | `quality-verifier` counter rebuilt from recorded budgets only ($0.00 instead of $4.09 of others' spend) |
| CI workflow | YAML valid (3 jobs); **not yet run on GitHub** |

## 6. Issues found and fixed

| Issue | How it was found | Fix |
|---|---|---|
| Simulation teams blocked by a seeded $2/day **feature** budget on `summarize`, worsening with each mode | Budget table: `support` blocked 36 → 71 → 120 across modes A, B, C | Workload features renamed; simulator warns on policies covering workload features; test against seeded names |
| Verifier budget over-attributed after restart ($4.09 vs $1 limit) | Containerized smoke run: verifications skipped | Record the charged budget per verification; reconcile by it |
| Demo data and Phase 1 smoke cases used budgeted shared features | Phase 1 smoke check got `402` instead of `503` | Renamed demo features; per-run smoke ids |
| Another worker (compose) could consume simulation jobs | Review before final runs | Private verification stream per run and mode |
| Reused run id started runs with spent budgets (phantom warnings) | Final runs showed 0 warnings where earlier runs had ~30 | Run-id reuse guard |
| Mock judge passes under-routed answers; `invalid_json` remainder | Report: "fail rate among under-routed: 0%"; failure breakdown | Reported as limitations; adequacy vs ground truth added; failures broken down by check |
| Python 3.12+ `sum()` / mock and similar details | — | (carried from earlier phases) |
| `asyncio.run()` inside a running loop; unclosed `gzip.open` | Trial run; ruff | `await`ed drain; `with` block |

## 7. Known limitations carried forward

Mock-model quality · sampling variance driven by a few long documents · synthetic, self-labelled
data · counterfactual baseline · `--real` mode implemented but not part of the results · CI not yet
run on GitHub · dashboard screenshots missing · no authentication. Plus everything listed for
Phases 2–5.
