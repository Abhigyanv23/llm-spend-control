# Phase 4 Theory Notes: Quality Checks and Escalation

Study notes for Phase 4. Each topic: the idea, why it matters, and where it shows up in the code.
Part 4A covers evaluation, sampling, judges and routing-as-classification. Parts 4B and 4C add
queues, workers, cascades and the cost of quality assurance.

---

## Part 4A

### 1. Offline vs online evaluation

- **Offline evaluation** runs a fixed test set (prompts with known good answers) through a model
  *before* release. It is repeatable and safe, but it only measures the traffic you thought of.
- **Online evaluation** measures quality on *live* traffic after release. It sees the real
  distribution (odd phrasings, new use cases, drift over time), but you don't know the right
  answer, and checking every request would cost as much as the requests themselves.

Phase 3 routed requests with rules written offline. Phase 4 adds online evaluation: re-check a
sample of real cheap answers against a stronger model. That's how you find out whether "cheap is
good enough" holds in production, not just in your test prompts.

→ `app/quality/` (sampling, judges); the worker in Part 4B.

### 2. Sampling and statistical confidence

You don't need to verify everything to know the miss rate, just as a poll doesn't ask every voter.
If you verify `n` randomly chosen answers and `x` fail, the estimated miss rate is
`p̂ = x / n`, with a **95% margin of error** of about

```
MoE ≈ 1.96 × √( p̂ (1 − p̂) / n )
```

Examples at a true miss rate of 10%:

| Verified (n) | Margin of error | Meaning |
|---|---|---|
| 100 | ±5.9 points | "between ~4% and ~16%": enough to spot a disaster |
| 400 | ±2.9 points | good enough to compare two routing policies |
| 900 | ±2.0 points | precise tracking per feature |

Margin of error shrinks with **√n**: four times the samples only halves the error. So 10% of
1,000 requests (n = 100) gives a rough estimate, while 10% of 100,000 is very precise. That's
why a *rate* is configured rather than a fixed count.

**Stratified (two-rate) sampling.** Uncertain decisions are where misses hide, so low-confidence
routes are sampled at 50% and confident ones at 10%. You learn more per verification dollar.
The catch: the overall miss rate must be **re-weighted** (each low-confidence sample counts
1/0.5, each confident one 1/0.1), or uncertain requests are over-represented.

→ `config/quality.yaml` `sampling`, `app/quality/sampling.py`.

### 3. Deterministic, hash-based sampling

`random.random() < rate` works, but:
- it isn't **reproducible**: a test or a replay can't predict which requests are sampled;
- retries of the same request may be sampled twice, or not at all.

Instead: `fraction = SHA-256(request_id) → first 64 bits / 2^64`, and sample if
`fraction < rate`. A good hash spreads ids uniformly over [0, 1), so across many requests the
sampled share converges to `rate` (`tests/test_sampling.py` checks this within 4 standard errors
over 20,000 ids). The same id always gives the same decision. Hashing a *stable key*
(request id, user id, tenant id) is also how feature flags and A/B tests assign groups.

### 4. LLM-as-judge

Asking a strong model "is this answer good?" scales far better than human review, but judges
have known **biases**:

| Bias | What happens | Mitigation used here |
|---|---|---|
| **Position** | Prefers whichever answer appears first (or second) | Fixed, labelled roles: REFERENCE vs CANDIDATE, not "A vs B" |
| **Verbosity** | Prefers longer answers | Rubric says "do not reward length; a shorter answer covering the same facts passes" |
| **Self-preference** | Prefers text written by its own model family | Unavoidable when the reference model also judges; monitor pass rates by model, and periodically spot-check with humans |
| **Format drift** | Doesn't return the JSON you asked for | `temperature = 0`, strict JSON instruction, robust parser, unusable output → `inconclusive` |

**Why a reference answer helps.** "Grade this answer" with no anchor makes the judge rely on its
own judgement, which is noisy. Giving it the strong model's answer turns the task into a
*comparison*, which is easier and more consistent. This is "reference-guided grading".

**Calibration.** A judge is calibrated when its score matches reality: of answers it scores
0.8, about 80% should truly be acceptable. Check this occasionally against a small set of
human labels. The parser also flags *internal* inconsistency (`verdict: pass` with
`score: 0.1`) as a cheap warning sign.

**Inconclusive is a real outcome.** A garbled judge reply must not count as pass or fail, or it
would bias the miss rate. It's recorded separately and excluded from the pass/fail ratio.

→ `app/quality/judges.py` (`JUDGE_SYSTEM_PROMPT`, `parse_judge_output`, `LLMJudge`).

### 5. A cheap deterministic judge for development

`SimilarityJudge` compares the cheap and reference answers with `difflib.SequenceMatcher` over
words (or Jaccard overlap of word sets) after stripping the `[mock:...]` tag. It is crude, since
two correct answers can be worded differently, but it is free, deterministic and good enough
to exercise the whole pipeline with mock models. Production uses `LLMJudge`. Same interface,
different strategy (the **Strategy pattern**).

Detail worth knowing: `SequenceMatcher` has `autojunk=True` by default, which silently ignores
"popular" items in sequences of 200+ elements and produces strange scores for natural text.
We pass `autojunk=False`.

### 6. Making the mocks fail realistically

A quality system is only testable if you can produce bad answers on demand.
- **Capability limits**: `mock-echo` only "understands" the first 200 characters,
  `mock-medium` 1,000, `mock-large` 4,000. Long prompts produce truncated cheap answers that the
  similarity judge fails, the way a small model loses track of a long document.
- **Directives** (`[[mock:empty]]`, `[[mock:refuse]]`, `[[mock:truncate]]`,
  `[[mock:badjson]]`), honoured only by the tier-1 mock, force each failure mode the escalation
  checks look for.
- Short prompts stay byte-for-byte identical, so every Phase 1–3 test still passes.

### 7. Routing as classification: precision, recall, asymmetric costs

Treat each routed request as a prediction: **"the cheap tier is enough" (positive)** or not.

| | Cheap really enough | Cheap NOT enough |
|---|---|---|
| **Routed cheap** | ✅ true positive: money saved | ❌ **false positive: a routing miss**, bad answer |
| **Routed strong** | 💸 false negative: overpaid | ✅ true negative |

- **Precision** of "cheap" = TP / (TP + FP): of the requests we sent cheap, how many were fine.
  `1 − precision` is the **miss rate** that Phase 4 measures.
- **Recall** of "cheap" = TP / (TP + FN): of the requests that *could* have gone cheap, how
  many did. Low recall means money left on the table.

The two errors don't cost the same. A false positive costs a bad answer (user trust, maybe a
legal risk); a false negative costs a few cents. So the threshold is set **asymmetrically**:
accept lower recall (some overpaying) to keep precision high, especially for risky features
(Phase 3's `min_tier: 3` for contract-review is exactly this). Verification data lets you set
that trade-off with evidence instead of guesses.

### 8. Privacy and data minimisation

Routing misses are valuable training data, and they contain **user prompts**, which may hold
personal or confidential information. Data minimisation means collecting only what you need,
for only as long as you need it:
- `privacy.store_prompts: false` keeps only the classifier *features* (keyword hits, size,
  has_code), never the text;
- `privacy.max_prompt_chars` caps what is stored;
- verification payloads live in Redis only until processed (bounded by `MAXLEN`).

Regulations like GDPR make this a legal requirement, not just good taste. Decide retention
*before* you have a table full of prompts.

---

## Self-check questions (Part 4A)

1. **You verified 100 cheap answers and 8 failed. What can you say about the miss rate?**
   About 8%, ±5.3 points (95%): somewhere around 3–13%. To halve that margin, verify 4× as many.

2. **Why hash the request id instead of calling `random()` to decide sampling?**
   Same input → same decision, so tests are reproducible and retries are consistent; a
   uniform hash still gives the configured rate across many requests.

3. **Name two LLM-judge biases and how this project mitigates them.**
   Position bias: fixed labelled REFERENCE/CANDIDATE roles. Verbosity bias: the rubric explicitly
   says not to reward length. (Format drift: temperature 0 + robust parsing → inconclusive.)

4. **Why does an unparseable judge reply become "inconclusive" rather than "fail"?**
   It says nothing about the cheap answer. Counting it as a fail (or a pass) would bias the
   measured miss rate.

5. **In routing terms, what is a "routing miss" and why is it costlier than the opposite error?**
   A false positive for "cheap is enough": the user got a bad answer. The opposite error
   (overpaying) only costs money, so precision of the cheap route is prioritised over recall.

6. **Why sample low-confidence routes at a higher rate, and what must you remember when
   reporting the overall miss rate?**
   Misses concentrate where the classifier is unsure, so you learn more per dollar there. The
   overall rate must be re-weighted by 1/rate per stratum, or it overstates misses.

---

## Part 4B

### 9. Message queues: why verification is asynchronous

Verifying an answer means two more model calls (reference + judge): seconds of latency and
real money. Doing it inside the request would make every user wait for a check that only
matters in aggregate. So the API **publishes a job and returns**, and a separate **consumer**
does the slow work later. The queue decouples them in time (the worker can be down for a
while), in rate (bursts pile up in the queue instead of overloading anything), and in failure
(a broken verifier never breaks a user request).

### 10. Delivery guarantees: at-most-once, at-least-once, exactly-once

| Guarantee | How | Risk |
|---|---|---|
| At-most-once | Ack *before* processing | A crash mid-job loses the job |
| **At-least-once** | Ack *after* the result is stored | A crash after storing but before acking → the job runs **twice** |
| Exactly-once | Needs the queue and the database in one transaction | Rarely truly available; usually emulated |

This project uses **at-least-once + idempotency**, the standard practical recipe:
"exactly-once *effect*" = at-least-once *delivery* + an **idempotent** consumer. The idempotency
key is `request_id`, enforced by a `UNIQUE` constraint on `verifications.request_id`:
- a cheap pre-check (`verification_exists`) skips the common case (redelivery after a crash)
  *before* paying for another reference call;
- the database constraint is the real guarantee: when two copies run *concurrently*, both pass
  the pre-check, and only one insert succeeds (`tests/test_worker.py` shows exactly this race).

Note what idempotency does NOT undo: the duplicate's model calls were still paid for.

### 11. Redis Streams in one page

| Concept | Command | Meaning |
|---|---|---|
| Append | `XADD stream MAXLEN ~ 10000 * field value` | Producer adds a job; `~` trims approximately (cheap) to bound memory |
| Consumer group | `XGROUP CREATE stream group 0 MKSTREAM` | Several workers share one logical consumer; each job goes to ONE of them |
| Read new | `XREADGROUP GROUP g consumer COUNT n BLOCK ms STREAMS s >` | `>` = never delivered before; the job enters this consumer's **Pending Entries List (PEL)** |
| Acknowledge | `XACK stream group id` | Done: removed from the PEL |
| Inspect | `XPENDING`, `XINFO CONSUMERS` | Who holds what, for how long, delivered how many times |
| Take over | `XAUTOCLAIM stream group consumer min-idle-ms 0-0` | Jobs pending longer than *min-idle* are reassigned; delivery count +1 |

The PEL plus `XAUTOCLAIM`'s *min-idle* time play the role of SQS's **visibility timeout**: a job
that isn't acked within `job_timeout_s` (worker crashed, or the attempt failed) becomes
visible to the group again.

### 12. Retries, poison messages and dead-letter queues

- **Transient failures** (timeouts, 429s, a provider blip) succeed if retried, so the worker
  leaves the job un-acked and it is redelivered after the timeout.
- **Poison messages** never succeed (malformed payload, a bug triggered by one input).
  Retrying forever wastes money and blocks the queue. So: unparseable → dead-letter
  immediately; anything else → after `max_attempts` deliveries → **dead-letter stream**,
  then ack. A human (or a replay script) inspects it later with
  `XRANGE quality:verify:dead - +`.
- The retry counter is the stream's own **delivery count** (`times_delivered` in `XPENDING`),
  so it survives worker restarts without any extra bookkeeping.

### 13. Backpressure and bounded concurrency

The worker reads at most `WORKER_CONCURRENCY` jobs at a time and processes them with
`asyncio.gather`. It never pulls more than it can handle. Unread jobs wait safely in Redis
(bounded by `MAXLEN`). This is **backpressure**: the slow consumer controls the pace, rather
than the fast producer flooding it. To go faster, run more worker processes. The consumer group
splits the jobs between them automatically.

Related lesson from building this: an idle loop must **sleep** when a poll returns nothing.
Redis blocks inside `XREADGROUP`, but an emulator (fakeredis) returned immediately, and the
loop then spun at 100% CPU and starved every other coroutine.

### 14. A separate worker process

Why `python -m app.worker` instead of a background task inside the API:
- **Isolation**: slow model calls never compete with user requests for the API's event loop.
- **Independent scaling**: 2 API processes and 6 workers, or the reverse.
- **Independent lifecycle**: redeploy or stop the verifier without touching the API.
- **Durability**: FastAPI `BackgroundTasks` die with the process; a queue doesn't.

Both processes build their dependencies with the same factory (`app/bootstrap.py`), so they
can't drift apart, and the worker never imports the web app.

**Graceful shutdown on Windows**: `loop.add_signal_handler()` is Unix-only, so the worker uses
`signal.signal(SIGINT, ...)` and passes the stop request to the event loop with
`call_soon_threadsafe`. The first Ctrl+C finishes the current batch; the second forces an exit.
Either way, nothing is lost: un-acked jobs stay pending and are reclaimed.

### 15. Why Redis Streams instead of Celery or RQ

- **Celery** doesn't officially support Windows and is synchronous-first (async code needs
  workarounds); it's also a lot of machinery for one queue.
- **RQ** forks a process per job: `fork()` doesn't exist on Windows.
- **Redis Streams** reuse the Redis we already run, are async-native through `redis.asyncio`,
  and expose the fundamentals (groups, acks, PEL, redelivery) instead of hiding them. That's
  ideal for learning, and fine at this scale. At larger scale: Kafka (ordered logs,
  replay), SQS / Cloud Tasks (managed), or Celery on Linux.

### 16. The cost of quality assurance (measured, not assumed)

Verification has a price: each sampled job pays for a **reference** call on a tier-3 model,
plus a **judge** call in production. In the Part 4B live run, **12 cheap requests cost
$0.00055 while verifying 8 of them cost $0.0476**, about 87× more. That's because the
reference uses a tier-3-priced model with a 1,024-token output budget, and 8/12 were sampled
(low-confidence rate 50%).

So the honest savings formula is:

```
true savings = baseline_cost − (routed_cost + escalation_cost + verification_cost)
verification_cost ≈ sample_rate × N × (reference_cost + judge_cost)
```

Implications:
- **Sampling rates are a budget decision.** 10% might be affordable; 50% of all cheap traffic
  might wipe out the savings. Use high rates briefly to learn, then turn them down.
- **Cap verification spend**, which is why it has its own budget (`quality-verifier`): when the
  budget is exhausted, verifications are recorded as `skipped`, not run.
- **Verification spend is visible** in the same audit/budget system as everything else.

---

## Self-check questions (Part 4B)

1. **Why ack only after the verification row is committed?**
   Ack-first means a crash between ack and store loses the job (at-most-once). Store-first
   means a crash re-delivers it: at-least-once, which is safe because storing is idempotent.

2. **Two copies of the same job run at the same moment. What prevents a duplicate row?**
   The UNIQUE constraint on `verifications.request_id`: the second INSERT fails, and the
   worker reports `duplicate`. The pre-check alone can't, because both copies pass it.

3. **What is a poison message and why not just retry it?**
   A job that can never succeed (e.g. malformed payload). Retrying burns money and blocks the
   queue. It goes straight to the dead-letter stream for a human to inspect.

4. **How does a crashed worker's job get processed?**
   It stays in the group's Pending Entries List. After `job_timeout_s` of idleness, another
   worker's `XAUTOCLAIM` takes it over (delivery count +1) and processes it.

5. **Why run verification in a separate process instead of FastAPI BackgroundTasks?**
   Isolation from user latency, independent scaling and deploys, and durability: background
   tasks die with the API process, while queued jobs survive.

6. **Verification cost 87× the requests it checked in our live run. What do you change?**
   Lower the sample rates (especially the 50% low-confidence rate), cap the verifier budget,
   lower `reference_max_tokens`, and report savings net of verification spend.

---

## Part 4C

### 17. Routing vs cascades

- **Routing** makes *one* guess up front ("this looks easy → tier 1") and lives with it.
- A **cascade** tries the cheap model first and *checks the answer*; if it looks broken, it
  retries on a stronger model. (FrugalGPT-style cascades are the research name.)

| | Routing only | Cascade |
|---|---|---|
| Cost when cheap is fine | 1 cheap call | 1 cheap call (+ a cheap check) |
| Cost when cheap fails | 1 cheap call, **bad answer** | 1 cheap + 1 strong call, **good answer** |
| Latency when cheap fails | normal | ~2× (two calls in sequence) |
| Needs | a classifier | a classifier + a way to *detect* failure |

A cascade only works if failures are **detectable cheaply and synchronously**. That's why the
post-call checks here are deterministic (empty, refusal, cut off, broken JSON). Subtle
wrongness (a fluent but incorrect answer) can't be caught in milliseconds; that's left to the
asynchronous verifier and to improving the router over time.

### 18. Pre-call vs post-call escalation

- **Pre-call** (prevention): an important request (`high`/`critical`) that the classifier is
  unsure about (confidence < 0.6) starts one tier higher. It costs **one** call, just a pricier
  one. Use it where a bad first answer is unacceptable *and* the uncertainty is known up front.
- **Post-call** (correction): pay for the cheap attempt, inspect it, retry once if it's visibly
  broken. Each attempt is reserved and settled **separately** (both cost real money). The audit
  row records the **sum** and lists every attempt in `metadata.escalation`.

**Where the code lives** (separation of concerns):
- *Policy* (when to escalate): `app/quality/escalation.py`, driven by `quality.yaml`.
- *Mechanism* (which model serves tier N): `Router.pick_from_tier`.
- *Orchestration* (reserve → call → settle each attempt, one audit row): the `Gateway`.

`/v1/route/preview` applies the same pre-call rule, so a dry run matches reality.

### 19. Graceful degradation

Escalation is an *improvement*, never a new way to fail. If the stronger model is blocked
by the budget, unavailable, or errors, the user gets **the answer we already have**, plus a
note (`metadata.escalation.blocked` / `.note`). The same principle as fail-open budgets and
best-effort enqueueing: optional quality features must not take the core path down.

`max_escalations: 1` bounds the worst case. Without it, a prompt that no model can satisfy
(e.g. `[[mock:badjson]]`, where even the strong mock doesn't return JSON) would escalate
until the budget ran out.

### 20. Measuring whether routing is safe

`GET /v1/quality` turns the stored verdicts into the numbers you'd show a stakeholder:
- **miss rate** = fail / (pass + fail) of verified cheap answers, with a **95% margin**
  (in our live data, 17 verifications gave 52.9% ± 23.7: wide, because n is small);
- **weighted miss rate**: each verdict counted 1/sample_rate times, correcting for
  over-sampling low-confidence routes;
- **per-model and per-feature** misses: where the router is wrong;
- **escalation rate**: how often the cascade had to step in (a rising rate is an early
  warning that the cheap model or the traffic changed);
- **net savings** = routing savings (already net of escalation cost) − verification spend.

A note on escalation and savings: a request that fails cheaply and then succeeds on tier 3
costs *more* than sending it to tier 3 directly (both calls are paid), so its savings can be
**negative**. The aggregate tells you whether the cascade still pays off.

### 21. Feedback loops: misses become training data

Every verified failure is a **labelled example**: the features the classifier saw, the tier it
chose, and the tier that was actually needed. `scripts/export_misses.py` writes them as JSONL.
That's the bridge from rules (`rules-v1`) to a learned classifier:
1. route with rules → 2. verify a sample → 3. store misses with labels →
4. train a classifier on them → 5. route with it → back to 2.

Caveats worth saying in an interview:
- **Selection bias**: you only get labels for what you sampled, and only "failures" are stored
  as misses. Train on passes too (they're in `verifications`).
- **Feedback loops can drift**: if the new classifier routes differently, the data it is
  verified on changes too. Keep a fixed random sample as a stable benchmark.
- **The judge's errors become label noise**: calibrate the judge before trusting its labels.

---

## Self-check questions (Part 4C)

1. **What's the difference between routing and a cascade, and when does a cascade cost more?**
   Routing guesses once; a cascade checks and retries. When the cheap attempt fails, you pay for
   both calls, which is more than going straight to the strong model.

2. **Why are the post-call checks so simple (empty, refusal, truncated, invalid JSON)?**
   They must run synchronously in milliseconds, for free. Subtle errors need a judge, which is
   too slow and expensive for the request path, so they go to async verification.

3. **The escalation is blocked by the budget. What does the user get, and why?**
   The original (cheap) answer, with `metadata.escalation.blocked`. Escalation is optional,
   so it degrades gracefully instead of turning a budget limit into an error.

4. **Why does pre-call escalation only apply to high/critical requests with low confidence?**
   It costs more on every such request. That's worth it only where a bad answer is expensive
   (high priority) and the router admits it's unsure.

5. **How is an escalated request recorded?**
   One `request_logs` row: model = the one that produced the returned answer,
   cost = the sum of all attempts, every attempt listed in `metadata.escalation.attempts`.
   Budget counters were settled per attempt. It is not sampled for async verification.

6. **What makes exported routing misses imperfect training data?**
   Selection bias (only sampled requests, only failures as misses), judge noise in the labels,
   and distribution shift once the new classifier changes what gets routed where.
