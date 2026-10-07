# Interview Notes

Likely questions about this project, with short answers to practise out loud. Numbers come from
the Phase 6 simulation (run `final-v2`) unless stated otherwise.

---

**1. What is this project, in two sentences?**
A gateway in front of LLM providers that routes each request to the cheapest model tier that can
handle it, enforces per-team and per-feature budgets before money is spent, and checks a sample of
cheap answers against a stronger model. On a 1,000-prompt workload it cut simulated spend by 40%
net of verification (64% gross) with 98% of requests served at or above their required tier.

**2. How do you enforce a budget when you only know the cost after the response?**
Reserve-then-settle, like a hotel card pre-authorisation. Before the call I reserve the worst
case (estimated input plus all of `max_tokens` at the output price). After the call I release the
hold and book the actual cost; on failure I release and book nothing. The hold makes in-flight
spend visible to concurrent requests, and `try/finally` guarantees it is always released.

**3. Why a Lua script in Redis?**
Check-and-reserve must be atomic. With `GET` then `INCRBY`, two requests can both read "room left"
and both reserve it: a time-of-check/time-of-use race. A Lua script runs as one indivisible step.
A test shows 50 concurrent naive reservations all succeeding where only 10 fit; the Lua version
admits exactly 10.

**4. Why integer nano-dollars in Redis and NUMERIC in Postgres?**
Floats can't represent most decimals (0.1 + 0.2 ≠ 0.3) and errors accumulate across millions of
tiny costs. Python uses `Decimal`, Postgres `NUMERIC(14,8)`, and Redis has no decimal type, so it
stores integers of 10⁻⁹ USD, where `INCRBY` is exact. JSON money is a fixed-point string.

**5. Postgres and Redis both hold spend: which is right?**
Postgres is the source of truth (the audit log); Redis counters are a fast cache that can drift
(restart, a crash between reserve and settle). Reconciliation rebuilds the counters from Postgres
at startup. I found a bug there: verification spend wasn't included, so every restart reset the
verifier's budget. Now each verification records which budget it was charged to.

**6. Fail-open or fail-closed when Redis is down?**
A business decision, so it's a setting. Open keeps the product working and risks overspend for a
while (logged loudly, header on every response); closed protects money and turns a Redis outage
into an outage of every AI feature.

**7. How does routing work, and why rules instead of ML?**
A rule-based classifier maps keywords, risk terms, code and input size to tier 1–3 with a
confidence and reasons; feature rules clamp it (contract review is always tier 3). Rules solve
the cold start: no labelled data exists on day one, and rules are explainable. The routing-miss
export is the labelled data that would train a learned classifier behind the same interface.

**8. Routing vs cascading?**
Routing guesses once up front. A cascade tries cheap first, checks the answer, and retries a tier
up if it's visibly broken. Cascading costs more when the cheap attempt fails (both calls are paid),
so I use it only for failures detectable in milliseconds: empty, refusal, cut off, invalid JSON.
In the simulation it rescued all 23 genuinely broken cheap answers.

**9. Which routing error matters more?**
Under-routing (a weaker model than the task needs) gives users bad answers; over-routing only
wastes money. So I report them separately, not just accuracy. The held-out split shows rules-v2
cut under-routing from 23 to 6 prompts out of 301, and over-routing from 27 to 17.

**10. How do you know the classifier improved and not just overfit?**
I designed the changes by reading *training-split* misses only, grouped them into generic patterns
(payload keywords, negation, reasoning questions), and measured once on a held-out split that is
split by *template*, so near-duplicate prompts can't leak across. 83.4% → 92.4% held-out accuracy.
Caveat: I wrote both the dataset and the classifier, so real traffic will be harder.

**11. How do you measure whether cheap answers are good enough in production?**
Sample a share of cheap answers, re-ask a tier-3 reference model, and have a judge compare them.
Report the pass rate with a Wilson confidence interval (97.4%, 93.4–99.0%, n = 152) and break misses
down by model and feature. Sampling is a deterministic hash of the request id, with higher rates
where the classifier is unsure.

**12. What are the weaknesses of LLM-as-judge?**
Position bias, verbosity bias, self-preference, and format drift. Mitigations: fixed labelled
roles, a rubric that says not to reward length, a reference answer to compare against,
temperature 0, and robust parsing where unusable output is "inconclusive", never a pass or fail.
And the judge can be blind: in the mock setup it passed every under-routed answer, which is why
I also score against ground truth.

**13. How does the verification queue work?**
Redis Streams with a consumer group: `XADD` with approximate `MAXLEN`, `XREADGROUP`, `XACK` only
after the result is committed, `XAUTOCLAIM` to take over jobs a crashed worker left pending, a
delivery-count retry limit, then a dead-letter stream. At-least-once delivery plus idempotency
(a UNIQUE `request_id`) gives exactly-once *effect*.

**14. Why not Celery?**
Celery isn't officially supported on Windows and is sync-first; RQ needs `fork()`. Streams reuse
the Redis already in the stack, are async-native, and expose the queue fundamentals directly.

**15. Gross vs net savings?**
Gross = baseline cost minus actual cost. Net also subtracts verification, which you only pay
because you route cheaply. Here: 63.7% gross, 40.3% net at a 10% sample rate, 55% at 5%, 12% at
50%. The sample rate is a budget decision.

**16. How does the monthly projection work, and where does it fail?**
Run-rate: month-to-date spend / elapsed days × days in month, next to a 7-day trailing average and
an EWMA. Run-rate overreacts to early spikes (a day-2 batch job gets multiplied by 15), so elapsed
time is floored at a day and the three forecasts are shown together. Seasonal models would be next.

**17. How did you make the experiment trustworthy?**
Same dataset for every mode, a server per mode with pinned configs, savings on the paired set,
provenance (git commit, dataset hash) in every report, and reports regenerable from results alone.
Three isolation bugs produced plausible but wrong numbers (a shared feature budget, a shared
stream, a reused run id); each was caught by checking the numbers, then fixed and guarded.

**18. What breaks first at scale?**
Analytics on the transactional database (move to a replica or warehouse with daily rollups);
reconciliation resetting in-flight holds across several gateway instances (needs a lock);
multi-key Lua scripts on Redis Cluster (hash tags); and a policy lookup per request (cache it).

**19. What security gaps remain?**
No authentication or authorisation: anyone who can reach the API can change budgets and read
usage, including stored prompt previews. Next: API keys or OIDC per team, role-based access for
budgets and analytics, and HMAC-keyed prompt fingerprints so they can't be brute-forced.

**20. What would you do next?**
Stratify verification sampling by request size (a few long documents caused most of the cost and
the run-to-run variance), train a learned classifier on the routing-miss data, run the `--real`
mode on a small slice with real providers and an LLM judge, and add auth.
