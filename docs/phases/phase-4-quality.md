# Phase 4: Quality Checks and Escalation

## Goal
Phase 3 routes cheap by default. Phase 4 measures whether that was good enough and fixes it
when it wasn't: sample cheap-routed answers for asynchronous verification against a stronger
model, record routing misses as labelled data, and escalate synchronously when a request is
high-stakes or the cheap answer is visibly broken.

## What was built
- `config/quality.yaml` (sampling, verification, escalation, privacy), validated at startup
- Deterministic hash-based sampling: 10% base, 50% when classifier confidence < 0.6;
  explicit/pinned, tier-3 and escalated requests are never sampled
- Verification queue on Redis Streams (consumer group, ack-after-store, `XAUTOCLAIM`,
  retries, dead-letter stream, `MAXLEN ~`)
- Worker process `python -m app.worker` (`--once`), bounded concurrency, graceful Ctrl+C on
  Windows, sharing `app/bootstrap.py` with the API
- Judges: `SimilarityJudge` (dev) and `LLMJudge` (production), one interface
- Verification budget (`quality-verifier` / `verification`), `skipped` when exhausted
- Tables `verifications` (UNIQUE `request_id`) and `routing_misses` (migration `0002`)
- Synchronous escalation: pre-call tier bump; post-call checks + one-step cascade
- Quality API (`/v1/quality`, `/misses`, `/queue`) and `scripts/export_misses.py`
- Mock capability limits and `[[mock:...]]` directives so failures can be produced for free

## Two loops, two time scales

| | Synchronous escalation | Asynchronous verification |
|---|---|---|
| When | Inside the request | Seconds to hours later, in the worker |
| Catches | *Visible* failures: empty, refusal, cut off, broken JSON | *Subtle* failures: fluent but wrong or incomplete |
| How | Deterministic checks (ms, free) | Reference model + judge (seconds, $) |
| Effect | The user gets a better answer now | The routing policy gets better later |
| Coverage | Every routed request | A sample |

## Design decisions

**Redis Streams, not Celery/RQ.** Celery doesn't officially support Windows and is sync-first;
RQ needs `fork()`. Streams reuse our Redis, are async-native through `redis.asyncio`, and
expose the fundamentals (groups, acks, pending entries, redelivery). At larger scale: Kafka,
SQS, or Celery on Linux.

**At-least-once + idempotency.** The worker acks only after the verification row is committed,
so a crash means redelivery, never loss. A cheap pre-check skips already-verified jobs before
paying for a reference call; the UNIQUE `request_id` is the real guarantee when two deliveries
race (the duplicate's model calls are still paid for).

**Retry with the queue's own delivery count.** `XAUTOCLAIM` increments `times_delivered`, so
retries survive worker restarts with no extra bookkeeping. Poison messages go straight to the
dead-letter stream; anything else after `max_attempts`.

**A separate process.** Verification is slow and optional. Running it in the API's event loop
would add latency and die with the API process; a worker scales and deploys independently.

**Deterministic sampling.** `SHA-256(request_id)` → [0, 1) < rate. Reproducible in tests and
stable across retries, yet converges to the configured rate.

**Stratified rates.** Misses hide where the classifier is unsure, so low-confidence routes are
sampled 5× more. `/v1/quality` reports a `miss_rate_weighted` that corrects for it.

**A reference answer for the judge.** Grading against a strong model's answer (reference-guided)
is more consistent than open-ended grading. The LLM judge runs at temperature 0, gets fixed
labelled roles (position bias), is told not to reward length (verbosity bias), and an
unparseable reply is `inconclusive`, never a pass or fail.

**Verification costs real money and has its own budget.** Reference + judge calls are
reserved and settled under `quality-verifier`; when it's exhausted, verifications are stored as
`skipped` rather than silently dropped. Reconciliation restores this spend from
`verifications` (it was missing at first: see the change record).

**Pre-call escalation is policy, the tier lookup is mechanism.** When to bump lives in
`app/quality/escalation.py` (driven by `quality.yaml`); which model serves tier N is
`Router.pick_from_tier`; the gateway orchestrates. `/v1/route/preview` applies the same rule.

**The cascade never fails a request.** A blocked, failed or impossible escalation returns the
answer we have, with a note. `max_escalations: 1` bounds cost and latency.

**One audit row per request.** `cost_usd` = sum of all attempts, `model` = the model that
produced the returned answer, `metadata.escalation.attempts` = the full story. No new request
status was needed (the CHECK constraint stays unchanged).

**Mocks that fail realistically.** Capability limits (200/1,000/4,000 chars) make long prompts
produce truncated cheap answers; directives force each escalation trigger. Short prompts are
byte-for-byte unchanged, so all Phase 1–3 tests still pass.

**Privacy by configuration.** `store_prompts: false` keeps only classifier features in
`routing_misses`; prompts are capped at `max_prompt_chars`; listings show a 200-char preview;
exports are git-ignored.

## Known limitations
- The similarity judge is only meaningful for the mocks; real answers need the LLM judge,
  whose biases (self-preference especially, when the reference model also judges) need
  periodic human spot-checks
- Post-call checks only catch visible failures; refusal detection is a phrase list
- Pre-call and post-call escalation each pick the *next* tier; there's no "jump to tier 3"
- A duplicate delivery that races past the pre-check pays for a second reference call
- Verification spend of a job that fails after the reference call (then dead-letters) is
  settled in Redis but has no `verifications` row, so reconciliation can't restore it
- Reconciliation charges all verification spend to the *current* verifier team/feature
- `/v1/quality` savings are computed from JSON metadata on every call (fine at this scale;
  a rollup table would be needed for millions of rows)
- The worker's job payload stores the full conversation in Redis until trimmed by `MAXLEN`
- No alerting on a rising miss rate, escalation rate or dead-letter count yet (Phase 5)
- Still no authentication: prompt previews are readable by anyone who can reach the API

## Key formulas
```
sampled            ⇔ SHA-256(request_id)[0:8] / 2^64 < rate
rate               = low_confidence_rate if confidence < low_confidence_threshold else base_rate
miss_rate          = fail / (pass + fail)
margin_95          = 1.96 × √(miss_rate × (1 − miss_rate) / (pass + fail))
miss_rate_weighted = Σ_fail (1/rate) / Σ_judged (1/rate)
escalated cost     = Σ attempts (each reserved and settled separately)
savings            = baseline_cost(final tokens) − total cost of all attempts
net_savings        = Σ savings − Σ verification_cost
verification_cost  ≈ sample_rate × N × (reference_cost + judge_cost)
```

## Interview talking points
- **How do you know routing is safe?** Verify a random sample against a stronger model, report
  the miss rate with its margin of error, weight for stratified sampling, break it down by
  model and feature. Measured, not assumed.
- **At-least-once + idempotency**: ack after commit; UNIQUE key absorbs redelivery; and the
  test that proves two concurrent deliveries produce one row.
- **True savings after verification overhead**: in dev runs, verification at a 50% sample rate
  cost 20–90× the requests it checked and turned net savings negative. Sampling rate is a
  budget decision; verification has its own capped budget.
- **Cascade vs routing**: cheap first, check, escalate on visible failure; costs more when the
  cheap attempt fails, so the aggregate (escalation rate, net savings) decides if it pays off.
- **Feedback loop**: routing misses are labelled examples (features + needed tier), exported
  as JSONL for a future learned classifier, with selection bias and judge noise as caveats.
- **A bug found by measuring**: the verifier budget was reset by every API restart because
  reconciliation only summed `request_logs`. Found by checking that the numbers add up.
