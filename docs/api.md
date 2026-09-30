# API Reference

Base URL: `http://127.0.0.1:8000`. Interactive docs are at `/docs`.

## `POST /v1/chat`

### Request
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `messages` | list of `{role, content}` | yes | — | role ∈ `system`, `user`, `assistant`; at least 1 |
| `team_id` | string | yes | — | Budget owner |
| `feature` | string | yes | — | Product feature making the call |
| `priority` | string | no | `normal` | `low`, `normal`, `high`, `critical` |
| `model` | string | no | registry default | Must exist in `/v1/models` |
| `max_tokens` | int | no | 512 | 1–8192 |
| `temperature` | float | no | 0.7 | 0.0–2.0 |

### Response `200`
```json
{
  "request_id": "uuid",
  "model": "mock-echo",
  "provider": "mock",
  "output": "text",
  "usage": { "input_tokens": 3, "output_tokens": 9 },
  "cost_usd": 0.0000039,
  "latency_ms": 52.4,
  "metadata": { "finish_reason": "stop", "raw_model": "mock-echo",
                "team_id": "search", "feature": "summarize", "priority": "normal" }
}
```

### Errors
All errors share this shape:
```json
{ "error": { "code": "provider_error", "message": "...", "retryable": true, "provider": "openai" } }
```

| Status | `code` | Cause |
|---|---|---|
| 400 | `unknown_model` | Model not in registry |
| 400 | `context_too_long` | Estimated tokens exceed model limit |
| 422 | — | Request body failed validation (FastAPI default shape) |
| 429 | `provider_error` | Upstream rate limit (retryable) |
| 502 | `provider_error` | Upstream failure |
| 503 | `provider_error` | Provider not configured (missing API key) |
| 504 | `provider_error` | Upstream timeout (retryable) |

## `GET /v1/models`
Returns `{ "models": [ ModelSpec, ... ] }` with name, provider, tier, prices per MTok, latency estimate, max context and supported features.

## `GET /health`
Returns `{ "status": "ok" }`.