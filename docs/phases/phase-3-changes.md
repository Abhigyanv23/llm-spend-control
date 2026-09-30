# Phase 3: Complete Change Record

Every change made in Phase 3 (Request Complexity Routing), file by file.

| | |
|---|---|
| Release | `v0.3.0` |
| Branch | `feat/phase-3-routing` (merged via pull request) |
| Previous state | `v0.2.0` (end of Phase 2) |
| Schema changes | None: routing data is stored in the existing `metadata` JSONB column |
| New dependencies | None |
| Verification | 93/93 pytest · 13/13 Phase 2 smoke test · 13/13 routing smoke test against Postgres 16 + Redis 7 (Docker) |

Related documents: [design decisions](phase-3-routing.md) · [theory notes](../notes/phase-3-theory.md) ·
[API reference](../api.md) · [architecture](../architecture.md) · [CHANGELOG](../../CHANGELOG.md)

---

## 1. Summary

Before Phase 3, every request used either the caller's `model` or a single default.
After Phase 3:

- **Requests without a model are routed by complexity** to one of three tiers.
- **Routing policy is configuration**, not code: `config/routing.yaml` holds tier candidates
  per profile, feature rules and classifier keywords, validated at startup.
- **Every decision is explainable**: tier, source, reasons, classifier features and
  confidence are returned and audited.
- **Budget pressure degrades before it denies**: a blocked request is retried on cheaper
  allowed tiers before a `402`.
- **Savings are measured per request** against "everything on the strongest model".
- **New APIs** to inspect the policy and dry-run routing decisions.

## 2. Behaviour changes

| Change | Before | After | Compatible? |
|---|---|---|---|
| Request without `model` | Always `defaults.model` (`mock-echo`) | Routed to tier 1/2/3 by the classifier and feature rules | Behaviour change: callers that relied on the default now get the routed model |
| Request with `model` | Used as-is | Used as-is (`source: explicit`, no downgrade) | Yes |
| `metadata.routing` in `/v1/chat` responses and audit rows | — | Added | Yes (additive) |
| `/health` | 4 fields | Adds `routing_profile` | Yes (additive); exact-match clients/tests need updating |
| Budget block for a routed request | Immediate `402` | `402` only after every allowed cheaper tier is also blocked | Behaviour change (intended) |
| Context too large for every routed candidate | n/a | `400 context_too_long` (same contract as Phase 1) | Yes |
| New error | — | `503 no_route` | Yes (new code) |

## 3. New behaviour at a glance

| Situation | Result |
|---|---|
| No keywords | Tier 1, confidence 0.5 |
| Keywords from one tier | That tier, confidence 0.85 |
| Keywords from several tiers | Highest tier, confidence 0.65 |
| Risk keyword (`contract`, `medical`, ...) | Tier 3, confidence 0.9 |
| Code present or input > `long_context_tokens` | At least tier 2 |
| Feature `min_tier` / `max_tier` | Tier clamped into the range; reason recorded |
| Feature `pin_model` | That model, `source: pinned` |
| `priority_min_tier` set for the priority | Tier floor raised (beats a feature cap) |
| No usable model in the tier | Escalate one tier at a time, never above `max_tier` |
| Nothing usable up to `max_tier`, only context too small | `400 context_too_long` |
| Nothing usable up to `max_tier`, other reason | `503 no_route` |
| Budget blocks the routed model, fallbacks exist | Next cheaper allowed model is tried; `metadata.routing.downgrades` lists blocked attempts |
| Budget blocks and the feature sets `budget_downgrade: false` | `402` as in Phase 2 |

## 4. Changes by file

### 4.1 Configuration

| File | Status | What changed |
|---|---|---|
| `config/routing.yaml` | new | Tier descriptions; profiles `dev` (mock per tier) and `production` (real providers) with ordered candidates and `baseline_model`; global `budget_downgrade`; `priority_min_tier` (empty by default); feature rules `contract-review` (min 3, no downgrade), `code-review` (min 2), `autocomplete` (max 1); classifier keywords per tier, risk and structured-output keywords, `long_context_tokens` |
| `config/models.yaml` | modified | Added `mock-medium` (tier 2, 1.00/5.00 per MTok) and `mock-large` (tier 3, 3.00/15.00); mocks share a 32k context so context errors are easy to test |
| `app/config.py` | modified | New settings `routing_config_path`, `routing_profile` |
| `.env.example` | modified | Added `ROUTING_CONFIG_PATH`, `ROUTING_PROFILE` |

### 4.2 Routing domain (`app/routing/`, all new)

| File | What it does |
|---|---|
| `config.py` | Dataclasses `FeatureRule`, `ClassifierConfig`, `RoutingConfig`; `load_routing_config()` validates profiles, tiers, models, feature bounds, pinned models and priority floors against the registry; `RoutingConfigError` |
| `classifier.py` | `RuleBasedClassifier` (`rules-v1`): whole-word keyword matching with optional plural suffix, instruction text = system prompts + latest user message, code detection, risk and structured-output detection; `RequestFeatures`, `Classification` (tier, confidence, reasons) |
| `router.py` | `Router.route()`: explicit > pinned > classifier within bounds; candidate filtering by provider availability, context window and capabilities; escalation capped at `max_tier`; budget-downgrade fallbacks; `RouteDecision`; `available_providers(settings)`; `baseline` property |
| `__init__.py` | Package exports |

### 4.3 Core application

| File | Status | What changed |
|---|---|---|
| `app/gateway.py` | modified | Pipeline now starts with `route()`. New `_reserve_with_downgrade()` retries the reservation on each fallback when `BudgetExceededError`/`OverrideRequiredError` is raised. Computes baseline cost and savings. Adds `metadata.routing` to responses and audit records (errors included). `Gateway(..., router=None)` keeps Phase 2 behaviour when no router is injected |
| `app/main.py` | modified | Lifespan loads and validates the routing config before opening connections, builds the `Router`, logs the profile and available providers, injects the router into the `Gateway`. Includes the routing API router. `/health` reports `routing_profile`. Version `0.3.0` |
| `app/api/routing.py` | new | `GET /v1/routing`, `POST /v1/route/preview` |
| `app/errors.py` | modified | New `NoRouteError` (`503 no_route`, audited as `provider_error`) |

### 4.4 Tests (33 new; 93 total)

| File | Status | Tests | Covers |
|---|---|---|---|
| `tests/test_classifier.py` | new | 9 | Default tier and confidence, single/mixed keywords, risk, code floor, long-input floor, whole-word and plural matching, instruction-only matching, structured-output flag |
| `tests/test_router.py` | new | 18 | Tier selection, fallbacks, explicit and unknown models, feature min/max, pinning, priority floor, provider availability, required capabilities, context too long, baseline, 5 invalid-config cases |
| `tests/test_routing_api.py` | new | 6 | End-to-end routing and savings, tier-3 routing, budget downgrade, no-downgrade feature (402), preview dry run, routing config endpoint |
| `tests/test_api.py` | modified | — | `/health` exact-match assertion includes `routing_profile` |

### 4.5 Scripts

| File | Status | What changed |
|---|---|---|
| `scripts/smoke_routing.py` | new | 13 live checks: health profile, routing config, 4 preview tiers, routed and explicit chats, savings, tiny-budget downgrade, no-downgrade 402, routing metadata in audit rows. Fresh `smoke3-<run id>` teams; `GATEWAY_URL` supported |

### 4.6 Documentation

| File | Status | What changed |
|---|---|---|
| `README.md` | modified | Phase 3 features, Routing section (tiers, decision order, preview example), 3 troubleshooting rows, routing settings, layout, limitations, doc links |
| `CHANGELOG.md` | modified | New `[0.3.0]` entry |
| `docs/architecture.md` | modified | Component diagram with router and routing policy, 9-step lifecycle with route and downgrade, routing decision flowchart, classifier signal table, sequence diagram with the downgrade loop, new design principles |
| `docs/api.md` | modified | `model` now optional-and-routed, `metadata.routing` reference, `503 no_route`, `/v1/route/preview`, `/v1/routing`, `/health` field |
| `docs/phases/phase-3-routing.md` | new | Goal, decision order, design decisions, known limitations, formulas, interview talking points |
| `docs/notes/phase-3-theory.md` | new | 12 theory topics + 6 self-check questions |
| `docs/phases/phase-3-changes.md` | new | This document |

## 5. New configuration

| Variable | Default | Used by |
|---|---|---|
| `ROUTING_CONFIG_PATH` | `config/routing.yaml` | app |
| `ROUTING_PROFILE` | `dev` | app |

## 6. New and changed endpoints

| Method | Path | Status |
|---|---|---|
| `POST` | `/v1/chat` | changed: routed when `model` is omitted; budget downgrade; `metadata.routing` |
| `GET` | `/health` | changed: reports `routing_profile` |
| `GET` | `/v1/models` | changed: lists `mock-medium`, `mock-large` |
| `GET` | `/v1/routing` | new |
| `POST` | `/v1/route/preview` | new |

New error code: `503 no_route`.

## 7. Issues found and fixed during verification

| Issue | How it was found | Fix |
|---|---|---|
| `test_health_and_models` failed after `/health` gained `routing_profile` | pytest (92/93) | Updated the exact-match assertion; documented the additive field. Exact assertions on small documented contracts are kept deliberately |

## 8. Verification results

| Check | Environment | Result |
|---|---|---|
| `python -m pytest` | fakeredis + SQLite | 93 passed (60 Phase 2 + 33 Phase 3) |
| `scripts/smoke_test.py` | PostgreSQL 16 + Redis 7 (Docker) | 13/13 (Phase 1 and 2 behaviour unchanged) |
| `scripts/smoke_routing.py` | PostgreSQL 16 + Redis 7 (Docker) | 13/13 |
| `/v1/route/preview` on 5 sample prompts | Live server, `dev` profile | Tiers 1, 1, 2, 3, 3 with confidences 0.5, 0.85, 0.85, 0.85, 0.9, as designed |

## 9. Known limitations carried forward

Rule-based classifier (English-only, plural-only stemming, blind to negation) · savings
baseline reuses the actual output token count · provider availability = API key present, not
healthy · no failover on provider errors · routing config needs a restart to change · routing
policy is global, not per tenant · blocked downgrade attempts still record a 100% budget
alert · no authentication. Plus everything listed for Phase 2.
Details in [phase-3-routing.md](phase-3-routing.md#known-limitations).