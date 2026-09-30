# Phase 3: Request Complexity Routing

## Goal
Send each request to the cheapest model that can handle it well, instead of sending
everything to the strongest model, while keeping explicit, auditable control over when
quality must win over cost.

## What was built
- Tiered routing: tier 1 extraction/formatting, tier 2 summarisation/classification,
  tier 3 reasoning-heavy or high-risk work
- `config/routing.yaml`: candidate models per tier (per profile), feature rules, classifier
  keywords; validated against the model registry at startup
- Rule-based classifier (`rules-v1`) with features, confidence and human-readable reasons
- Router with a fixed decision order: explicit model > pinned feature > classifier tier
  within feature bounds and optional priority floor
- Candidate filtering by provider availability, context window and required capabilities
- Budget-aware downgrade: at a budget limit, fall back to a cheaper allowed tier before blocking
- Counterfactual baseline cost and savings stored on every request
- `GET /v1/routing` and `POST /v1/route/preview` (dry run)
- Mock models priced like each real tier (`mock-echo`, `mock-medium`, `mock-large`)

## Decision order
1. **explicit**: `model` in the request is honoured as-is
2. **pinned**: a feature rule with `pin_model` is used as-is
3. **routed**: the classifier picks a tier, clamped to `[min_tier, max_tier]` of the feature
   rule and the optional `priority_min_tier` floor. The first candidate in that tier that is
   available, fits the context window and has the required capabilities is chosen. If none
   qualifies, escalate upward, never above `max_tier`
4. Cheaper allowed tiers (never below `min_tier`) become budget-downgrade fallbacks

## Design decisions

**Rules before ML.** No labelled data exists on day one (cold start). Rules are explainable
and tunable in config. The classifier sits behind a `classify()` interface, so a learned
model trained on Phase 4's routing-miss data can replace it without touching the router.

**Optimistic default, pessimistic on risk.** No signal routes to tier 1 with confidence 0.5;
risk keywords go straight to tier 3 with confidence 0.9. Under-routing a risky request is
far more expensive than over-routing a simple one.

**Instructions only.** Keywords are matched in system prompts plus the latest user message.
Earlier turns are context and would add noise.

**`max_tokens` is not a complexity signal.** It predicts cost, not difficulty. It already
drives the worst-case budget estimate.

**Priority is not risk.** Priority governs budget overrides (Phase 2); risk governs model
quality. `priority_min_tier` exists but is off by default.

**Mechanism versus policy.** The router is code; `routing.yaml` is policy. Behaviour changes
are config edits, validated at startup (fail fast).

**Degrade before deny.** A blocked reservation holds nothing (all-or-nothing Lua), so trying a
cheaper model's smaller estimate is safe. `min_tier` and `budget_downgrade: false` protect
features where quality must not drop.

**Caps are real.** The router never escalates above `max_tier`; it returns `503 no_route`.
A too-large prompt still returns Phase 1's `400 context_too_long`.

**Profiles.** `dev` maps tiers to mock models priced like real ones, so savings maths is
realistic and tests are deterministic; `production` maps to real providers.

**No schema change.** Routing data lives in the existing `metadata` JSONB column.

## Known limitations
- Keyword classifier: English-only, no stemming beyond a plural suffix, blind to negation
  ("don't analyze")
- Baseline savings price the *actual* output tokens on the strongest model; that model might
  have produced a different number of tokens
- Provider availability means "API key set", not "provider healthy"; no failover on provider
  errors yet (Phase 4)
- Routing config is loaded at startup; changes need a restart
- Routing policy is global, not per team or tenant
- Blocked attempts during a downgrade still record a 100% budget alert
- Still no authentication on any endpoint

## Key formulas
tier = clamp(classifier_tier, max(feature.min_tier, priority_floor), feature.max_tier)

baseline_cost = baseline_model.cost(actual_input_tokens, actual_output_tokens)

savings = baseline_cost − actual_cost

## Interview talking points
- Routing versus cascading: predict up front (one call) versus try cheap and escalate
  (quality-checked, sometimes two calls); this project does routing now, cascading in Phase 4
- Asymmetric error costs drive the defaults (cheap by default, strong on risk)
- Explainability: every decision stores its reasons, features and confidence
- Budget-aware downgrade combines Phase 2 enforcement with Phase 3 routing: graceful
  degradation instead of a hard 402
- Honest measurement: the savings baseline is counterfactual, and its limitation is known