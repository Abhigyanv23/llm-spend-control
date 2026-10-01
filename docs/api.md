# API Reference

Base URL: `http://127.0.0.1:8000`. Interactive docs are at `/docs`.

**Money format.** Every money field is a `Decimal` serialised as a fixed-point JSON **string**
with 8 decimal places (`"0.00000430"`), never a float. Parse it with a decimal type
(`decimal.Decimal` in Python, `BigDecimal` in Java, a decimal library in JS).

**Authentication.** None yet: every endpoint, budget changes and stored prompt previews
included, is open to anyone who can reach the server. Known limitation, planned for a later
phase.

## `POST /v1/chat`

### Request headers
| Header | Required | Notes |
|---|---|---|
| `Content-Type: application/json` | yes | |
| `X-Budget-Override` | no | Free-text reason (max 500 chars). Lets a `high`/`critical` request proceed past an exhausted budget; stored in the audit log. Ignored for `low`/`normal`. |

### Request body
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `messages` | list of `{role, content}` | yes | — | role ∈ `system`, `user`, `assistant`; at least 1 |
| `team_id` | string | yes | — | Budget owner. `^[A-Za-z0-9._-]+$`, max 64 |
| `feature` | string | yes | — | Product feature making the call. Same format as `team_id`. Also selects the feature's routing rule |
| `priority` | string | no | `normal` | `low`, `normal`, `high`, `critical` |
| `model` | string | no | routed | If set, must exist in `/v1/models` and is used as-is (no routing, no downgrade). If omitted, the router picks one |
| `max_tokens` | int | no | 512 | 1–8192. Also sets the **worst-case budget reservation** |
| `temperature` | float | no | 0.7 | 0.0–2.0 |

### Response `200`
Headers: `X-Budget-Warning` is present when any limit is at ≥ 80% (after this request's
estimate), when an override was used, or when budgets could not be checked:

```
X-Budget-Warning: team:search:day=85.2%; feature:summarize:month=91.0%
X-Budget-Warning: team:demo-tiny:day=160.1% (overridden)
X-Budget-Warning: budget-check-unavailable
```

```json
{
  "request_id": "4a9c8d3c-d642-4714-9fa2-aa112a7320c3",
  "model": "mock-echo",
  "provider": "mock",
  "output": "[mock:mock-echo] You said: Hello gateway",
  "usage": { "input_tokens": 3, "output_tokens": 10 },
  "cost_usd": "0.00000430",
  "latency_ms": 57.88,
  "metadata": {
    "finish_reason": "stop", "raw_model": "mock-echo",
    "team_id": "search", "feature": "summarize", "priority": "normal",
    "budget": { "status": "warning", "estimated_cost_usd": "0.00020550" },
    "budget_warnings": [
      { "kind": "warning", "scope": "team", "scope_id": "search", "period": "day",
        "period_key": "2026-09-30", "limit_usd": "5.00000000", "spent_usd": "4.10000000",
        "reserved_usd": "0.00000000", "estimated_cost_usd": "0.00020550",
        "projected_usd": "4.10020550", "percent_used": 82.0,
        "resets_at": "2026-10-01T00:00:00Z" }
    ],
    "routing": {
      "model": "mock-echo", "tier": 1, "source": "routed", "min_tier": 1, "max_tier": 3,
      "fallbacks": [],
      "reasons": [],
      "classifier": {
        "name": "rules-v1", "tier": 1, "confidence": 0.5,
        "reasons": ["no complexity signals: defaulting to the cheapest tier"],
        "features": { "input_tokens": 7, "message_count": 1, "has_code": false,
                      "structured_output": false, "keyword_hits": {}, "risk_hits": [] }
      },
      "final_model": "mock-echo", "final_tier": 1,
      "downgrades": [],
      "baseline_model": "mock-large",
      "baseline_cost_usd": "0.00015900",
      "savings_usd": "0.00015470"
    }
  }
}
```

`metadata.budget.status` is one of `ok`, `warning`, `overridden`, `unchecked` (fail-open).
`budget_warnings[].kind` is `warning`, `overridden`, or `unchecked`.

`metadata.routing`:

| Field | Meaning |
|---|---|
| `model`, `tier` | The router's choice before any budget downgrade |
| `source` | `explicit` (caller's `model`), `pinned` (feature rule), `routed` (classifier) |
| `min_tier`, `max_tier` | Bounds from the feature rule and the priority floor |
| `fallbacks` | Cheaper models the gateway may downgrade to under budget pressure |
| `reasons` | Why the tier was constrained (feature rule, clamping, escalation) |
| `classifier` | Present for `routed`: tier, confidence (0–1), reasons and extracted features |
| `final_model`, `final_tier` | The model that actually served the request |
| `downgrades` | Each budget-blocked attempt: `model`, `tier`, `blocked_by`, `estimated_cost_usd` |
| `baseline_model`, `baseline_cost_usd` | The same tokens priced on the strongest model |
| `savings_usd` | `baseline_cost_usd − cost_usd` (a counterfactual estimate) |

Failed requests store `metadata.routing` in the audit log too (without baseline or savings).
For an escalated request, `final_model`/`final_tier` are the model that produced the returned
answer, and `savings_usd` is computed against the **total** cost of all attempts (it can be
negative).

`metadata.escalation` (Phase 4), present on every successful response:

```json
"escalation": {
  "pre_call": null,
  "attempts": [
    { "model": "mock-echo", "tier": 1, "cost_usd": "0.00000050",
      "estimated_cost_usd": "0.00040090", "input_tokens": 5, "output_tokens": 0,
      "latency_ms": 64.12, "finish_reason": "stop", "check_failed": "empty" },
    { "model": "mock-medium", "tier": 2, "cost_usd": "0.00006500",
      "estimated_cost_usd": "0.00500900", "input_tokens": 5, "output_tokens": 12,
      "latency_ms": 81.37, "finish_reason": "stop", "check_failed": null,
      "escalated_because": "empty" }
  ],
  "escalated": true, "escalations": 1, "final_check_failed": null,
  "blocked": null, "note": null, "extra_cost_usd": "0.00006500"
}
```

| Field | Meaning |
|---|---|
| `pre_call` | `null` if the rule didn't apply; else `{applied, from_model, from_tier, to_model, to_tier, reason}` (`applied: false` with a reason when it applied but couldn't bump) |
| `attempts` | Every provider call made for this request, in order, with its cost and the post-call check it failed (`empty`, `refusal`, `truncated`, `invalid_json`, or `null`) |
| `escalated`, `escalations` | Whether (and how many times) the post-call cascade replaced the answer |
| `final_check_failed` | The check the *returned* answer still fails (e.g. after `max_escalations`), or `null` |
| `blocked` | Set when the escalation was blocked by a budget: `{model, tier, blocked_by, estimated_cost_usd}`; the original answer is returned |
| `note` | Why no (further) escalation happened, e.g. `not escalated: the model was chosen by 'explicit'` |
| `extra_cost_usd` | Cost of the escalation attempts (already included in `cost_usd`) |

`cost_usd` and `usage` of an escalated request are the **sums over all attempts**.

`metadata.quality.sampling` (Phase 4): whether this answer was queued for asynchronous
verification.

```json
"quality": { "sampling": { "sampled": true, "rate": 0.5,
                           "reason": "low-confidence rate 50%: sampled", "fraction": 0.003162 } }
```
`fraction` is the request id's hash in [0, 1); the answer is sampled when `fraction < rate`.
Ineligible requests (explicit/pinned model, tier 3, escalated, verification disabled) have
`rate: 0` and a reason.

### Errors
All gateway errors share this shape (`request_id` matches the audit-log row):
```json
{ "error": { "code": "provider_error", "message": "...", "retryable": true,
             "provider": "openai", "request_id": "uuid" } }
```

Budget errors add the limit that was hit (the most-exceeded one if several were):
```json
{
  "error": {
    "code": "budget_exceeded",
    "message": "Team 'demo-tiny' daily budget of $0.0005 reached: spent $0.00043 + reserved $0.00 + this request's estimate $0.0002055 = $0.0006355 (127.1% of limit). Resets at 2026-10-01T00:00:00Z.",
    "retryable": false,
    "scope": "team", "scope_id": "demo-tiny", "period": "day", "period_key": "2026-09-30",
    "limit_usd": "0.00050000", "spent_usd": "0.00043000", "reserved_usd": "0.00000000",
    "estimated_cost_usd": "0.00020550", "projected_usd": "0.00063550",
    "percent_used": 127.1, "resets_at": "2026-10-01T00:00:00Z",
    "priority": "normal", "limits_exceeded": ["team:demo-tiny:day"],
    "request_id": "uuid"
  }
}
```
When routing allowed a downgrade, a budget error is only returned after every cheaper
allowed model was also blocked; the details describe the last (cheapest) attempt.

| Status | `code` | Cause |
|---|---|---|
| 400 | `unknown_model` | Model not in registry |
| 400 | `context_too_long` | Estimated tokens exceed the limit of the chosen model (or of every allowed candidate when routed) |
| 402 | `budget_exceeded` | A team/feature daily or monthly limit would be reached (`low`/`normal` priority), after any allowed downgrade. Not retryable before `resets_at` |
| 402 | `override_required` | Same, for `high`/`critical` priority without `X-Budget-Override`. Retry with the header to proceed |
| 422 | — | Request body failed validation (FastAPI default shape) |
| 429 | `provider_error` | Upstream rate limit (retryable) |
| 500 | `internal_error` | Unexpected gateway bug (logged with traceback) |
| 502 | `provider_error` | Upstream failure |
| 503 | `provider_error` | Provider not configured (missing API key) for an explicit or pinned model |
| 503 | `no_route` | No available model in the allowed tiers satisfies the request (API keys, required capabilities) |
| 503 | `budget_unavailable` | Redis/Postgres unreachable and `BUDGET_FAIL_MODE=closed` (retryable) |
| 503 | `database_unavailable` | Postgres unreachable on a read endpoint (`/v1/usage`, `/v1/budgets`, `/v1/quality`) (retryable) |
| 503 | `queue_unavailable` | Redis unreachable on `/v1/quality/queue` (retryable) |
| 504 | `provider_error` | Upstream timeout (retryable) |

A failed or budget-blocked **escalation** never produces an error: the original answer is
returned with `metadata.escalation.blocked` / `.note`.

**Why 402 and not 429 or 403?** 429 means "too many requests, slow down": a rate limit that
clears in seconds, which clients retry automatically. 403 means "you are not allowed", a
permissions problem. A spent budget is neither: the caller is authorised and not too fast,
but their money has run out until the period resets or someone raises the limit.
`402 Payment Required` says exactly that, and clients won't blindly retry it.

## `POST /v1/route/preview`
Dry run: which model **would** serve this request, and why. Same body as `/v1/chat`.
No provider call, no budget reservation, no audit row. Use it to tune `config/routing.yaml`.

```json
{
  "routing": { "model": "mock-large", "tier": 3, "source": "routed", "min_tier": 1,
               "max_tier": 3, "fallbacks": ["mock-medium", "mock-echo"], "reasons": [],
               "classifier": { "name": "rules-v1", "tier": 3, "confidence": 0.85,
                               "reasons": ["tier-3 keywords: analyze, trade-offs"],
                               "features": { "...": "..." } } },
  "estimated_input_tokens": 14,
  "worst_case_cost_usd": "0.00772200",
  "baseline_model": "mock-large",
  "baseline_worst_case_cost_usd": "0.00772200"
}
```
Errors are the same as for `/v1/chat` routing: `400 unknown_model`, `400 context_too_long`,
`503 no_route`, `422` validation.

The preview applies the same pre-call escalation as `/v1/chat` and reports it in
`pre_call_escalation` (`null` when the rule doesn't apply), so a `high`/`critical` low-confidence
request previews on the bumped tier.

## `GET /v1/quality`
How safe is cheap routing, and is it still saving money? Aggregates `verifications`,
`routing_misses` and `request_logs` over a time window.

| Query param | Type | Notes |
|---|---|---|
| `team_id`, `feature` | string | Exact match (applies to all sections) |
| `from` | ISO 8601 datetime | Inclusive. Default: 7 days ago. Naive values are UTC |
| `to` | ISO 8601 datetime | Exclusive. Default: now |

```json
{
  "window": { "from": "2026-09-24T15:44:02Z", "to": null },
  "filters": { "team_id": null, "feature": null },
  "verification": {
    "verified": 17, "pass": 8, "fail": 9, "inconclusive": 0, "skipped": 0,
    "rates": { "pass": 0.4706, "fail": 0.5294, "inconclusive": 0.0, "skipped": 0.0 },
    "miss_rate": 0.5294, "miss_rate_margin_95": 0.2373, "miss_rate_weighted": 0.5294,
    "by_model": [ { "model": "mock-echo", "judged": 17, "fail": 9, "miss_rate": 0.5294 } ],
    "verification_cost_usd": "0.15538500"
  },
  "misses": { "total": 9, "by_model": [ { "model": "mock-echo", "count": 9 } ],
              "by_feature": [ { "feature": "check4b", "count": 5 } ] },
  "requests": { "successful": 24, "sampled": 9, "sample_rate": 0.375 },
  "escalation": { "pre_call_escalations": 1, "post_call_escalations": 4,
                  "escalation_rate": 0.1667, "escalation_extra_cost_usd": "0.00028800" },
  "costs": { "requests_cost_usd": "0.00350000", "verification_cost_usd": "0.15538500",
             "verification_overhead_pct": 4439.57, "gross_savings_usd": "0.03100000",
             "net_savings_usd": "-0.12438500" }
}
```
(Illustrative dev-profile values. The negative net savings is real behaviour at these sample
rates: verifying long prompts with a tier-3 reference costs far more than the cheap requests.)

| Field | Meaning |
|---|---|
| `miss_rate` | `fail / (pass + fail)`: share of verified cheap answers that a stronger model's answer showed were not good enough. `inconclusive` and `skipped` are excluded |
| `miss_rate_margin_95` | Normal-approximation 95% margin of error: `1.96 × √(p(1−p)/n)` |
| `miss_rate_weighted` | Each verdict weighted by `1 / sample_rate`, correcting for over-sampled low-confidence routes |
| `verification_cost_usd` | Reference + judge spend (charged to the `quality-verifier` budget) |
| `escalation_rate` | Post-call escalations / successful requests |
| `gross_savings_usd` | Sum of `metadata.routing.savings_usd` (already net of escalation cost) |
| `net_savings_usd` | `gross_savings_usd − verification_cost_usd` |

Rates are `null` when their denominator is 0.

## `GET /v1/quality/misses`
Routing misses (cheap answers that failed verification), newest first. The listing shows only
a 200-character `prompt_preview`; the full stored prompt is only in `scripts/export_misses.py`.

| Query param | Type | Notes |
|---|---|---|
| `team_id`, `feature` | string | Exact match |
| `model` | string | The cheap model that missed |
| `from`, `to` | ISO 8601 | Default window: last 7 days |
| `limit` | int | 1–500, default 50 |
| `offset` | int | ≥ 0 |

```json
{
  "items": [
    { "request_id": "5b57b889-...", "created_at": "2026-10-01T15:43:26.481100+00:00",
      "team_id": "smoke4-17666d14", "feature": "smoke4", "prompt_chars": 2000,
      "prompt_preview": "point0 point1 point2 ...",
      "chosen_model": "mock-echo", "chosen_tier": 1,
      "better_model": "mock-large", "better_tier": 3,
      "reason": "sequence similarity 0.168 < threshold 0.8",
      "classifier_confidence": 0.5,
      "classifier_features": { "input_tokens": 608, "has_code": false, "...": "..." } }
  ],
  "total": 2, "limit": 50, "offset": 0, "has_more": false
}
```
`prompt_preview` / `prompt_chars` are `null` when `privacy.store_prompts` is `false`.

## `GET /v1/quality/queue`
Health of the verification queue (Redis Streams).
```json
{ "stream": "quality:verify", "consumer_group": "verifiers", "length": 22, "pending": 0,
  "dead_letter_stream": "quality:verify:dead", "dead_letter": 0,
  "consumers": [ { "name": "LAPTOP-0HV28H9P-33068", "pending": 0, "idle_ms": 669 } ] }
```
- `length`: entries in the stream (acked history included, capped by `MAXLEN ~`)
- `pending`: delivered to a worker but not yet acked (in progress, or waiting to be reclaimed)
- `dead_letter`: jobs that failed `max_attempts` times or were malformed
- Before any worker has run, the consumer group doesn't exist yet: `pending` is 0 and `consumers` empty.

## `GET /v1/routing`
The active routing policy and the providers currently usable (API key set, or keyless).
```json
{
  "profile": "dev",
  "tiers": {
    "1": { "description": "Extraction and formatting", "models": ["mock-echo"] },
    "2": { "description": "Summarisation and classification", "models": ["mock-medium"] },
    "3": { "description": "Reasoning-heavy or high-risk work", "models": ["mock-large"] }
  },
  "baseline_model": "mock-large",
  "budget_downgrade": true,
  "priority_min_tier": {},
  "features": {
    "contract-review": { "min_tier": 3, "max_tier": 3, "pin_model": null, "requires": [],
                         "budget_downgrade": false,
                         "reason": "Legal review: correctness matters more than cost" }
  },
  "available_providers": ["mock", "ollama"]
}
```

## `GET /v1/usage`
Reads the audit trail, newest first.

| Query param | Type | Notes |
|---|---|---|
| `team_id`, `feature`, `model` | string | Exact match |
| `status` | string | `success`, `provider_error`, `budget_blocked`, `validation_error`, `internal_error` |
| `from` | ISO 8601 datetime | Inclusive. Naive values are treated as UTC |
| `to` | ISO 8601 datetime | Exclusive |
| `limit` | int | 1–500, default 50 |
| `offset` | int | ≥ 0, default 0 |

```json
{
  "items": [
    { "request_id": "uuid", "created_at": "2026-09-30T10:14:57.100Z", "team_id": "search",
      "feature": "summarize", "priority": "normal", "model": "mock-echo", "provider": "mock",
      "input_tokens": 3, "output_tokens": 10, "estimated_cost_usd": "0.00020550",
      "cost_usd": "0.00000430", "latency_ms": 57.9, "status": "success",
      "error_code": null, "override_reason": null,
      "metadata": { "routing": { "...": "..." }, "...": "..." } }
  ],
  "totals": { "count": 128, "cost_usd": "0.00061200", "input_tokens": 410, "output_tokens": 1290 },
  "limit": 50, "offset": 0, "has_more": true
}
```
`totals` cover **all** rows matching the filters, not just the current page. Rows are written
by a background task just after each response, so a row can appear a few milliseconds after
its `/v1/chat` response. `model` is the model that actually served the request (after any
downgrade).

## `GET /v1/budgets`
```json
{ "policies": [ { "scope": "team", "scope_id": "search", "daily_limit_usd": "5.00000000",
                  "monthly_limit_usd": "100.00000000", "enabled": true,
                  "updated_at": "2026-09-30T10:09:13Z" } ] }
```

## `PUT /v1/budgets/{scope}/{scope_id}`
Create or replace a policy. `scope` is `team` or `feature`. Takes effect on the next request.

```json
{ "daily_limit_usd": "5.00", "monthly_limit_usd": null, "enabled": true }
```
- `null` limit = unlimited for that period; `"0"` = no spend allowed.
- Limits: ≥ 0, at most 14 digits with 8 decimal places (send strings to avoid float rounding).
- Returns the stored policy (same shape as in `GET /v1/budgets`). 422 on invalid input.

## `GET /v1/budgets/{scope}/{scope_id}/status`
Live counters from Redis. Works for any team/feature, with or without a policy.
```json
{
  "scope": "team", "scope_id": "search",
  "policy": { "scope": "team", "scope_id": "search", "daily_limit_usd": "5.00000000",
              "monthly_limit_usd": "100.00000000", "enabled": true, "updated_at": "..." },
  "day":   { "period": "day", "period_key": "2026-09-30", "spent_usd": "0.00000430",
             "reserved_usd": "0.00000000", "limit_usd": "5.00000000", "percent_used": 0.0,
             "remaining_usd": "4.99999570", "resets_at": "2026-10-01T00:00:00Z" },
  "month": { "period": "month", "period_key": "2026-09", "...": "..." }
}
```
`percent_used = (spent + reserved) / limit × 100`; `reserved` is money held by in-flight
requests. Returns `503 budget_unavailable` if Redis is unreachable.

## `POST /v1/budgets/reconcile`
Rebuilds all current-period Redis counters from `request_logs` (the same thing that runs at
startup). Resets every in-flight hold to 0, so run it when traffic is quiet.
```json
{ "periods": ["2026-09-30", "2026-09"], "counters_rebuilt": 26,
  "reconciled_at": "2026-09-30T10:14:54.395620Z" }
```

## `GET /v1/models`
Returns `{ "models": [ ModelSpec, ... ] }` with name, provider, tier, prices per MTok (strings),
latency estimate, max context and supported features. Includes the mock models
`mock-echo`, `mock-medium` and `mock-large` (tiers 1–3) used by the `dev` routing profile.

## `GET /health`
Always `200` while the process is up; reports dependencies without failing on them.
```json
{ "status": "ok", "postgres": "ok", "redis": "ok", "budget_fail_mode": "open",
  "routing_profile": "dev" }
```
`status` is `degraded` when a dependency is down, e.g. `"redis": "down (ConnectionError)"`.