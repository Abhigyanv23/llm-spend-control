# Phase 1 Theory Notes: Unified Request Gateway

## 1. The gateway (reverse proxy) pattern
Without a gateway, every service calls OpenAI or Anthropic directly: nobody can see total
spend, enforce a budget, or switch models without editing every caller. A gateway is a single
service that all LLM calls pass through. Callers talk to *your* API; your API talks to the
providers. One choke point means logging, budgets, routing and caching live in one place.
Same idea as API gateways in microservices (Kong, AWS API Gateway), specialised for LLMs.
Real-world examples: LiteLLM, Portkey, OpenRouter.

## 2. Canonical schema (normalisation)
Every provider's API has a different shape:
- OpenAI puts the system prompt inside `messages` and reports `prompt_tokens` / `completion_tokens`.
- Anthropic takes `system` as a separate field and reports `input_tokens` / `output_tokens`.
- Ollama reports `prompt_eval_count` / `eval_count`.

Define **one internal request format and one response format** and translate at the edges.
This is an **anti-corruption layer**: external quirks never leak into core logic, so new
features don't multiply in complexity with every provider.

## 3. The adapter pattern
Each provider gets a class implementing the same interface:
`complete(request, model) -> ProviderResult`. The gateway only calls `adapter.complete()` and
never knows which provider is behind it (**polymorphism**: depend on the abstraction
`ProviderAdapter`, not a concrete class). Adding a provider means one new file and one line
in `build_adapters()`: the **Open/Closed Principle** (open for extension, closed for
modification).

## 4. The model registry (config as data)
Prices, context lengths, quality tiers and capabilities change often. Hard-coded in Python,
every price change is a deploy. In YAML (later a database table) they are **data, not code**.
The registry is also what makes routing possible: "give me the cheapest tier-2 model that
supports tools" is a registry query.

## 5. Token-based cost math
LLMs bill per token, with input and output priced differently. Output is usually 3-5x more
expensive because generation is sequential and compute-heavy. Prices are quoted per million
tokens (MTok):

    cost = (input_tokens × input_price_per_MTok + output_tokens × output_price_per_MTok) / 1,000,000

Key consequence: **input tokens are known before the call, output tokens only after.** This is
the root of the "enforce budgets before, know cost after" problem solved in Phase 2.

Money and floats: `0.1 + 0.2 != 0.3` in binary floating point. Phase 1 used floats as a
stopgap; Phase 2 moved to `Decimal`, `NUMERIC(14,8)` and integer nano-dollars. Real billing
systems never sum floats.

## 6. Why async (FastAPI + httpx)
An LLM call spends ~99% of its time waiting on the network: **I/O-bound**, not CPU-bound.
Synchronous code blocks a worker for 2-10 seconds per request. With `async`/`await`, while one
request waits on a provider, the same thread serves hundreds of others (the **event loop**).
A proxy is the textbook case for async.

Use **one shared `httpx.AsyncClient`**: it keeps a **connection pool**, so repeated calls to
the same host reuse TCP/TLS connections instead of a new handshake each time (~50-200 ms saved
per call). It is created and closed in the FastAPI **lifespan**, once per process.

## 7. Measuring latency correctly
Use `time.perf_counter()`, not `time.time()`. `time.time()` is wall-clock time and can jump
when the system clock syncs. `perf_counter` is **monotonic** and high-resolution, made for
measuring durations.

## 8. Error normalisation
Clients get the same error shape whatever went wrong upstream:
`{"error": {"code", "message", "retryable", ...}}`. Each error is marked **retryable** or not:
- Retryable (transient): 429 rate limit, 5xx server errors, timeouts.
- Not retryable (will fail the same way): 400 bad request, 401 bad key.

An upstream 401 is returned to our caller as **502**, not 401: the caller's request was fine,
*our* configuration (the API key) is broken. Passing 401 through would wrongly tell the caller
to fix their credentials. This flag later drives fallback and escalation logic.

## 9. Smoke tests
A **smoke test** is a quick, shallow check that the main paths work at all ("power it on and
see if smoke comes out"). It tests the **error paths** as well as the happy path: a gateway's
value is largely in how predictably it fails, so 400 / 422 / 503 responses are features.

## 10. Environment hygiene (lessons from setup)
- A **virtual environment** isolates each project's packages.
- `python -m <tool>` (pip, uvicorn, pytest) runs the tool from the *active* interpreter.
  A bare `uvicorn` runs whatever the shell finds first on `PATH`, possibly a different Python.
- `ModuleNotFoundError: No module named 'X'` → the module isn't installed / wrong environment.
  `ImportError: cannot import name 'Y' from 'X'` → the file exists but doesn't define `Y`.
  The first means check your environment; the second means check the file's contents.
- In Windows PowerShell 5.1, `curl` is an alias for `Invoke-WebRequest`; use
  `Invoke-RestMethod` or a Python script.

## 11. Conventional Commits
`type: short summary` + blank line + body explaining what and why. Types: `feat`, `fix`,
`docs`, `refactor`, `test`, `chore`. Tools can generate changelogs and version bumps from it
(`feat` → minor, `fix` → patch), and the `git log` tells the project's story at a glance.
Summary line under ~72 characters, imperative mood ("add", not "added").

## Self-check questions
1. Why do adapters return token counts but not cost?
   *Cost depends on registry prices. Computing it centrally means a price change is one YAML
   edit, not four code changes.*
2. What breaks if a new `httpx.AsyncClient` is created inside every request?
   *No connection reuse: every call pays a fresh TCP + TLS handshake, adding latency, and
   unclosed clients can leak sockets.*
3. Why is an upstream 401 returned to our caller as 502?
   *The caller did nothing wrong; the gateway's own credentials are broken. 502 (bad gateway)
   points at the upstream dependency, not the caller.*
4. Input tokens are countable before the call, output tokens aren't. How can the worst-case
   cost be bounded before the call?
   *Assume the model uses all of `max_tokens`: input estimate × input price + max_tokens ×
   output price. This is the Phase 2 reservation amount.*
5. Why async for a proxy?
   *Requests are I/O-bound; the event loop serves many requests concurrently while each one
   waits on the network.*
6. Why `perf_counter` instead of `time.time()` for latency?
   *It is monotonic: a system clock adjustment can't produce negative or inflated durations.*