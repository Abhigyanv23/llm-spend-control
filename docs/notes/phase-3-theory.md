# Phase 3 Theory Notes: Request Complexity Routing

## 1. Why routing saves money
Most production traffic is simple (extraction, formatting, short summaries). A tier-3 model
often costs 20-30x more per token than tier 1. If most requests can safely go to cheaper
tiers, the blended cost drops sharply while hard requests still get the best model.
Routing is usually the biggest single lever on an LLM bill.

## 2. Three strategies for choosing a model
- **Routing**: predict the right model before calling. One call per request; can mis-predict.
- **Cascading**: call the cheap model, check the answer, escalate if bad (FrugalGPT-style).
  Better quality, but some requests pay twice.
- **Learned routers**: a model trained on labelled "which model handled this well" data
  (RouteLLM line of work). Needs data first.

## 3. Rules first (cold start)
With no labelled data, a rule-based classifier is the practical start. It is explainable
(every decision has reasons) and tunable in config. Behind a fixed `classify()` interface
(Strategy pattern), it can later be swapped for a learned model trained on routing misses.

## 4. Feature engineering
Turn text into measurable signals: tier keywords, risk keywords, code presence, input size,
structured-output requests. Match keywords only in the instructions (system prompt + latest
user message). `max_tokens` is deliberately not a feature: it predicts cost, not difficulty.

## 5. Asymmetric errors and confidence
Over-routing wastes money; under-routing produces bad answers. Their costs differ by use
case, so risk escalates immediately while "no signal" defaults cheap with low confidence.
Confidence tells Phase 4 which decisions to verify first.

## 6. Policy layering
Precedence: explicit model > pinned feature > feature bounds > priority floor > classifier.
The router is mechanism; routing.yaml is policy.

## 7. Priority versus risk
Priority = how much the business needs the request to run (budget overrides).
Risk = how bad a wrong answer is (model quality). Different axes, different controls.

## 8. Hard constraints
Within a tier, pick the first candidate that is available, fits the context window and has
the required capabilities. Escalate only if the tier has none, and never above max_tier.

## 9. Graceful degradation
At a budget limit, downgrade to a cheaper allowed model before blocking. Safe because a
blocked reservation holds nothing. Guarded by min_tier and per-feature opt-out.

## 10. Counterfactual baseline
Savings = (same tokens priced on the strongest model) - actual cost. An estimate: the strong
model might have produced a different number of output tokens.

## 11. Explainability
Reasons, features, confidence and downgrades are stored with every request, so "why did this
go to the cheap model?" is a query, not a guess. Phase 4 mines this for routing misses.

## 12. Fail fast on configuration
Validate routing.yaml against the registry at startup. A typo stops the server with a clear
message instead of causing errors on live traffic.

## Self-check questions
1. Why does "no keywords" go to tier 1 but "contract" go to tier 3?
   *Under-routing risk is expensive; otherwise default cheap and verify later.*
2. Why is downgrading after a budget block safe?
   *The reserve script is all-or-nothing: a blocked attempt holds nothing.*
3. Why isn't max_tokens a complexity feature?
   *It predicts cost, not difficulty, and already drives the worst-case estimate.*
4. Why not escalate above max_tier when nothing is available?
   *A cap that silently breaks isn't a cap; return a clear 503 instead.*
5. What's the weakness of the savings baseline?
   *It reuses the actual output token count for a model that might have written more or less.*
6. How would you replace the rule-based classifier with ML?
   *Collect routing-miss labels (Phase 4), train a model on the same features, and plug it
   in behind the same classify() interface.*