"""Synchronous escalation policy: a CASCADE on top of routing.

Routing makes one up-front guess. A cascade can correct it:
  pre-call   an important request (high/critical) that the classifier is unsure about starts
             one tier higher. Costs nothing extra: still one call, just a stronger model.
  post-call  the cheap answer is checked for failures that are visible without a judge
             (empty, refusal, cut off, broken JSON). If one is found, the request is retried
             once on the next tier up and the user gets the better answer.
This module is the POLICY (when to escalate). The Router supplies the MECHANISM (which model
serves tier N) and the Gateway orchestrates the calls, budgets and audit trail.
"""
import json
import re
from dataclasses import replace

from app.quality.config import EscalationConfig
from app.routing import RouteDecision, Router, RuleBasedClassifier
from app.routing.classifier import Classification
from app.schemas import ChatRequest

# How providers say "I stopped because I hit max_tokens"
TRUNCATION_REASONS = frozenset({"length", "max_tokens"})
FENCED_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
REFUSAL_WINDOW = 300     # refusals come first; don't flag a long answer that quotes one later


def apply_pre_call_escalation(router: Router | None, cfg: EscalationConfig,
                              request: ChatRequest,
                              decision: RouteDecision) -> tuple[RouteDecision, dict | None]:
    """Returns the (possibly bumped) decision and metadata describing what happened (None when
    the rule doesn't apply at all)."""
    if router is None or not cfg.enabled or decision.source != "routed":
        return decision, None
    if request.priority.value not in cfg.pre_call_priorities:
        return decision, None
    classification = decision.classification
    if classification is None or classification.confidence >= cfg.pre_call_confidence_below:
        return decision, None

    why = (f"priority '{request.priority.value}' with low classifier confidence "
           f"({classification.confidence} < {cfg.pre_call_confidence_below})")
    if decision.tier >= decision.max_tier:
        return decision, {"applied": False, "reason": f"{why}, but already at max tier "
                                                      f"{decision.max_tier}"}
    spec = router.pick_from_tier(request, decision.tier + 1, decision.max_tier)
    if spec is None:
        return decision, {"applied": False, "reason": f"{why}, but no usable higher-tier model"}

    # The original choice becomes the first budget-downgrade fallback (if allowed)
    fallbacks = ((decision.model, *decision.fallbacks)
                 if router.downgrade_enabled(request.feature) else ())
    note = f"pre-call escalation: {why}: tier {decision.tier} -> {spec.tier}"
    bumped = replace(decision, model=spec.name, tier=spec.tier, fallbacks=fallbacks,
                     reasons=(*decision.reasons, note))
    return bumped, {"applied": True, "from_model": decision.model, "from_tier": decision.tier,
                    "to_model": spec.name, "to_tier": spec.tier, "reason": why}


def _is_valid_json(text: str) -> bool:
    match = FENCED_JSON.match(text)
    candidate = match.group(1) if match else text
    try:
        json.loads(candidate)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def check_answer(cfg: EscalationConfig, request: ChatRequest, output: str,
                 finish_reason: str | None,
                 classification: Classification | None) -> str | None:
    """The first post-call check that fails, or None. Cheap, deterministic checks only:
    anything subtler is the asynchronous verifier's job."""
    checks = cfg.post_call_checks
    text = output or ""
    if "empty" in checks and not text.strip():
        return "empty"
    if "truncated" in checks and (finish_reason or "").lower() in TRUNCATION_REASONS:
        return "truncated"
    if "refusal" in checks:
        head = text[:REFUSAL_WINDOW].lower().replace("’", "'")
        if any(pattern in head for pattern in cfg.refusal_patterns):
            return "refusal"
    if ("invalid_json" in checks and classification is not None
            and classification.features.structured_output
            and "json" in RuleBasedClassifier.instruction_text(request).lower()
            and not _is_valid_json(text)):
        return "invalid_json"
    return None
