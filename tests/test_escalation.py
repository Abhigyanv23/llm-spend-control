"""Escalation policy: post-call checks and the pre-call tier bump (unit level)."""
import dataclasses

import pytest

from app.quality import load_quality_config
from app.quality.escalation import apply_pre_call_escalation, check_answer
from app.registry import ModelRegistry
from app.routing import Router, load_routing_config
from app.schemas import ChatRequest, Message

REGISTRY = ModelRegistry("config/models.yaml")
ROUTING = load_routing_config("config/routing.yaml", "dev", REGISTRY)
ROUTER = Router(REGISTRY, ROUTING, {"mock", "ollama"})
CFG = load_quality_config("config/quality.yaml", REGISTRY, ROUTING,
                          {"mock", "ollama"}).escalation


def req(text: str, priority: str = "normal", feature: str = "f1") -> ChatRequest:
    return ChatRequest(team_id="t1", feature=feature, priority=priority,
                       messages=[Message(role="user", content=text)])


def check(text: str, output: str, finish_reason: str | None = "stop", cfg=CFG):
    request = req(text)
    return check_answer(cfg, request, output, finish_reason,
                        ROUTER.route(request).classification)


# ------------------------------------------------------------------ post-call checks

@pytest.mark.parametrize("output, finish_reason, expected", [
    ("", "stop", "empty"),
    ("   \n ", "stop", "empty"),
    ("half an ans", "length", "truncated"),           # OpenAI / Ollama wording
    ("half an ans", "max_tokens", "truncated"),       # Anthropic wording
    ("I'm sorry, but I can't help with that request.", "stop", "refusal"),
    ("I’m sorry, but I can’t help with that.", "stop", "refusal"),   # curly quotes
    ("A perfectly good answer.", "stop", None),
])
def test_visible_failures(output, finish_reason, expected):
    assert check("Hello", output, finish_reason) == expected


def test_refusal_quoted_late_in_a_long_answer_is_not_a_refusal():
    answer = "Here is the summary you asked for. " * 20 + "(Do not reply 'i can't help with')"
    assert check("Hello", answer) is None


@pytest.mark.parametrize("text, output, expected", [
    ("Return JSON with the fields", '{"name": "x"', "invalid_json"),
    ("Return JSON with the fields", '{"name": "x"}', None),
    ("Return JSON with the fields", '```json\n{"name": "x"}\n```', None),   # fenced is fine
    ("Make a table of the results", "not json at all", None),   # structured, but not JSON
    ("Hello", "not json at all", None),                         # nobody asked for JSON
])
def test_invalid_json_only_when_json_was_requested(text, output, expected):
    assert check(text, output) == expected


def test_disabled_checks_are_skipped():
    only_empty = dataclasses.replace(CFG, post_call_checks=("empty",))
    assert check("Hello", "I'm sorry, but I can't do that", cfg=only_empty) is None


# ------------------------------------------------------------------ pre-call escalation

def test_high_priority_low_confidence_starts_one_tier_up():
    request = req("Hello there", priority="high")
    decision = ROUTER.route(request)
    assert (decision.tier, decision.classification.confidence) == (1, 0.5)
    bumped, meta = apply_pre_call_escalation(ROUTER, CFG, request, decision)
    assert (bumped.model, bumped.tier) == ("mock-medium", 2)
    assert meta == {"applied": True, "from_model": "mock-echo", "from_tier": 1,
                    "to_model": "mock-medium", "to_tier": 2,
                    "reason": "priority 'high' with low classifier confidence (0.5 < 0.6)"}
    assert bumped.fallbacks[0] == "mock-echo"        # the original is the budget fallback
    assert "pre-call escalation" in bumped.reasons[-1]


@pytest.mark.parametrize("text, priority", [
    ("Hello there", "normal"),                       # not an escalation priority
    ("Summarize this article", "high"),              # confident (0.85): no bump
])
def test_pre_call_rule_does_not_apply(text, priority):
    request = req(text, priority=priority)
    decision = ROUTER.route(request)
    bumped, meta = apply_pre_call_escalation(ROUTER, CFG, request, decision)
    assert bumped is decision and meta is None


def test_pre_call_respects_the_feature_max_tier():
    request = req("Hello there", priority="critical", feature="autocomplete")   # max_tier 1
    decision = ROUTER.route(request)
    bumped, meta = apply_pre_call_escalation(ROUTER, CFG, request, decision)
    assert bumped.tier == 1 and meta["applied"] is False and "max tier 1" in meta["reason"]


def test_explicit_model_is_never_bumped():
    request = req("Hello there", priority="critical").model_copy(update={"model": "mock-echo"})
    decision = ROUTER.route(request)
    assert apply_pre_call_escalation(ROUTER, CFG, request, decision) == (decision, None)
