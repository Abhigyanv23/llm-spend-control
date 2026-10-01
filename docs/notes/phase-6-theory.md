# Phase 6 Theory Notes: Simulation, Evaluation and Release

Study notes for Phase 6. Each topic: the idea, why it matters, and where it shows up.

---

## Part 6A: datasets and labels

### 1. Offline evaluation datasets and ground-truth labels

To score a router you need to know the *right* answer for each request: the **ground-truth
tier**. Without labels you can measure cost, but not whether a cheap route was a mistake.
`data/workload.jsonl` has 1,000 prompts, each from a template with a human-assigned tier.
Labels are judgements, so state who made them and how (here: one author, by task type), and
keep them stable: changing labels mid-experiment changes the target you're measuring.

### 2. Train / test / held-out splits and leakage

- **Train** split: look at it freely to design improvements.
- **Test (held-out)** split: look at it *once*, at the end, to report the result.
- **Leakage**: any way test information influences the model. The obvious kind is tuning on
  test data. The subtle kind is **near-duplicates**: two prompts from one template differ only in
  a name or number, so a per-*prompt* split puts the "same" example in both splits. Here the split
  is **per template** (grouped split), ~30% of each category's templates held out.
- Being honest about it: the dataset and the classifier share an author, so the tricky categories
  were designed knowing the classifier's weaknesses, and an overall per-category accuracy check
  was run on all data before v2 was designed. The v2 rules themselves came only from training-
  split misses and are generic (negation, payload, question form), not template phrases.

### 3. Synthetic data: what it can and can't prove

Template-generated prompts are cheap, reproducible and labelled, but narrow: real traffic has
typos, mixed languages, odd lengths and tasks nobody anticipated. A classifier that scores 92% here
will score lower on real traffic. Use synthetic data to compare *versions* and catch regressions,
not to claim an absolute production accuracy.

## Part 6B: running experiments

### 4. Load testing: concurrency vs rate, open vs closed models

- **Closed model** (fixed concurrency): N clients, each sends its next request when the previous
  one returns. Throughput adapts to latency, so a slow server gets *less* load (it hides overload).
- **Open model** (fixed arrival rate): requests arrive at R/s whether or not earlier ones finished,
  like real users. A slow server builds a queue, which is how real outages happen.
- The simulator combines both: a **token bucket** caps the arrival rate (R/s with bursts up to the
  bucket size), and a **semaphore** caps concurrency, so neither the client nor the server is
  overwhelmed on a laptop.

### 5. Retries: exponential backoff with jitter

Retry only *retryable* failures (429, 502–504, connection errors; never a 402 budget block).
Wait `random(0, min(cap, base × 2^attempt))` before each retry: **exponential** so a struggling
server gets breathing room, **jittered** so many clients don't retry in lockstep (a "thundering
herd").

### 6. Experiment design on a fixed dataset (A/B/C)

Compare systems on the **same inputs**, changing one thing at a time:
- A = all on the strongest model (the counterfactual baseline),
- B = routing only,
- C = routing + verification + escalation.
Each mode gets its own server process with a **pinned config** saved in the report folder, and its
own team ids. **Isolation** matters: this project found three ways runs contaminated each other
(a shared feature budget, a shared verification stream, reused run ids), each producing
plausible-looking but wrong numbers. **Paired comparison**: savings are computed only over requests
that succeeded in *both* modes, so a mode can't look cheaper just by blocking more.

## Part 6C: reporting

### 7. Confusion matrices, per-tier precision/recall, asymmetric errors

Rows = true tier, columns = predicted tier. The diagonal is correct; below it (predicted < true) is
**under-routing**, a weaker model than needed (the *dangerous* error); above it is
**over-routing** (the *wasteful* error). Per tier: precision = of requests sent to tier t, how many
needed t; recall = of requests that needed t, how many got it. Because errors aren't equally costly,
one accuracy number is not enough: report under- and over-routing separately.

### 8. Confidence intervals and what the quality signal can see

The verification pass rate gets a 95% **Wilson** interval (e.g. 97.4%, CI 93.4–99.0%, n = 152).
But a precise number can still measure the wrong thing: with mock models the similarity judge only
detects length/format failures. It passed **every** under-routed answer it saw (fail rate among
under-routed verified answers: 0%). So the report also scores served tiers against the ground
truth ("adequately served"), which is what the judge *should* be approximating. With real models
an LLM judge is needed to see under-routing.

### 9. Reproducibility

- **Seeds**: the dataset is byte-identical for a seed (a test checks the committed file).
- **Pinned configs**: each run saves the exact `routing.yaml` and per-mode `quality.yaml` it used.
- **Provenance**: `run.json` records the git commit and the dataset's SHA-256.
- **Regenerable reports**: `build_report.py` needs only `results.jsonl.gz`, no server.
- **Remaining randomness**: request ids are random UUIDs, so *which* requests are sampled for
  verification changes per run. At a 10% rate, net savings varied from ~40% to ~53% between runs,
  because a few 27,000-character documents dominate verification cost. Report variance, don't pick
  the best run.

### 10. Honest reporting and counterfactual baselines

"Savings" are relative to a baseline that never ran in production: the same tokens priced on the
strongest model. The strongest model might have written a different number of tokens, and mock
answers have simulated quality. Say so next to the number. Gross vs net matters as much: here
routing saved 63.7% gross, and 40.3% after paying for verification at a 10% sample rate.

## Part 6D: improving the classifier

### 11. Evidence-driven improvement

Look at the *training* misses, group them by pattern, fix patterns rather than examples:
keywords inside the payload ("...to review the plan" is data, not the task), negation
("don't analyze"), and open reasoning questions with no keywords. Stop when the remaining misses
are deliberate trade-offs (the code floor) or would need example-specific rules. Then measure once
on the held-out split: 83.4% → 92.4% accuracy, under-routing 23 → 6.

## Part 6E: shipping

### 12. Containers vs local processes; compose networking and profiles

- One image, three services (gateway, worker, dashboard) with different commands: same code,
  one build.
- **Service names, not 127.0.0.1**: inside a container, 127.0.0.1 is the container itself; other
  services are reached by name (`postgres`, `redis`, `gateway`) on the compose network.
- **Profiles**: `docker compose up` starts only Postgres + Redis (the local-dev workflow);
  `--profile full` adds the app. A one-shot `migrate` service runs Alembic and the seed, and the
  app services wait for `service_completed_successfully`.
- `.env` holds host-side URLs (127.0.0.1); compose overrides them for containers.

### 13. CI basics

On every push and PR: **lint** (ruff), **test matrix** (pytest on Python 3.11 and 3.13, catching
version-specific breakage), and **service containers** (real Postgres + Redis) for the smoke tests,
the same checks that are run by hand on a laptop, automated. CI must be green before tagging.

### 14. Semantic versioning and release notes

MAJOR.MINOR.PATCH: breaking changes bump MAJOR, features MINOR, fixes PATCH. 0.x means "anything
may change"; **1.0.0** declares the API stable enough to depend on. Release notes summarise, for
users, what changed and what to do about it (migrations, new settings, breaking changes).

---

## Self-check questions

1. **Why split train/test by template instead of by prompt?**
   Prompts from one template are near-duplicates; a per-prompt split leaks them into both splits
   and inflates test accuracy.

2. **The judge passed 100% of under-routed answers. What does that tell you?**
   The quality signal can't see the dangerous error (a limitation of the mock/similarity setup).
   Measure against ground truth, and use an LLM judge with real models.

3. **Why compute savings on the paired set?**
   A mode that blocks more requests (they cost $0) would otherwise look cheaper.

4. **Net savings at a 10% sample rate varied from ~40% to ~53% between identical runs. Why, and
   what would you change?**
   A few very long documents dominate verification cost, and which ones get sampled is random.
   Stratify sampling by request size, or cap the verification cost per request.

5. **Why do containers use `postgres:5432` but your laptop uses `127.0.0.1:5432`?**
   Inside a container 127.0.0.1 is the container itself; compose's network resolves service names.

6. **What did isolation failures look like in this project?**
   A shared feature budget made results depend on run order; a shared stream let another worker
   verify jobs with the wrong config; a reused run id started a run with spent budgets. All
   produced plausible numbers: only checking them caught it.
