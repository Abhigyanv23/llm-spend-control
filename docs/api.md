# API Reference

Base URL: `http://127.0.0.1:8000`. Interactive docs are at `/docs`.

**Money format.** Every money field is a `Decimal` serialised as a fixed-point JSON **string**
with 8 decimal places (`"0.00000430"`), never a float. Parse it with a decimal type
(`decimal.Decimal` in Python, `BigDecimal` in Java, a decimal library in JS).

**Authentication.** None yet: every endpoint, budget changes included, is open to anyone who
can reach the server. Known limitation, planned for a later phase.

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
| `feature` | string | yes | — | Product feature making the call. Same format as `team_id` |
| `priority` | string | no | `normal` | `low`, `normal`, `high`, `critical` |
| `model` | string | no | registry default | Must exist in `/v1/models` |
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
    ]
  }
}
```

`metadata.budget.status` is one of `ok`, `warning`, `overridden`, `unchecked` (fail-open).
`budget_warnings[].kind` is `warning`, `overridden`, or `unchecked`.

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

| Status | `code` | Cause |
|---|---|---|
| 400 | `unknown_model` | Model not in registry |
| 400 | `context_too_long` | Estimated tokens exceed model limit |
| 402 | `budget_exceeded` | A team/feature daily or monthly limit would be reached (`low`/`normal` priority). Not retryable before `resets_at` |
| 402 | `override_required` | Same, for `high`/`critical` priority without `X-Budget-Override`. Retry with the header to proceed |
| 422 | — | Request body failed validation (FastAPI default shape) |
| 429 | `provider_error` | Upstream rate limit (retryable) |
| 500 | `internal_error` | Unexpected gateway bug (logged with traceback) |
| 502 | `provider_error` | Upstream failure |
| 503 | `provider_error` | Provider not configured (missing API key) |
| 503 | `budget_unavailable` | Redis/Postgres unreachable and `BUDGET_FAIL_MODE=closed` (retryable) |
| 504 | `provider_error` | Upstream timeout (retryable) |

**Why 402 and not 429 or 403?** 429 means "too many requests, slow down": a rate limit that
clears in seconds, which clients retry automatically. 403 means "you are not allowed", a
permissions problem. A spent budget is neither: the caller is authorised and not too fast,
but their money has run out until the period resets or someone raises the limit.
`402 Payment Required` says exactly that, and clients won't blindly retry it.

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
      "error_code": null, "override_reason": null, "metadata": { "...": "..." } }
  ],
  "totals": { "count": 128, "cost_usd": "0.00061200", "input_tokens": 410, "output_tokens": 1290 },
  "limit": 50, "offset": 0, "has_more": true
}
```
`totals` cover **all** rows matching the filters, not just the current page. Rows are written
by a background task just after each response, so a row can appear a few milliseconds after
its `/v1/chat` response.

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
latency estimate, max context and supported features.

## `GET /health`
Always `200` while the process is up; reports dependencies without failing on them.
```json
{ "status": "ok", "postgres": "ok", "redis": "ok", "budget_fail_mode": "open" }
```
`status` is `degraded` when a dependency is down, e.g. `"redis": "down (ConnectionError)"`.
