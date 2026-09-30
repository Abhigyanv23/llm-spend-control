import pytest

from app.errors import ContextTooLongError, NoRouteError, UnknownModelError
from app.registry import ModelRegistry
from app.routing import Router, RoutingConfigError, load_routing_config
from app.schemas import ChatRequest, Message

ROUTING_YAML = """
profiles:
  test:
    tiers: {1: [mock-echo], 2: [mock-medium], 3: [mock-large]}
  keyed:
    tiers:
      1: [gpt-4o-mini, mock-echo]
      2: [claude-haiku-4-5, mock-medium]
      3: [claude-sonnet-5-5, mock-large]
    baseline_model: claude-sonnet-5-5
budget_downgrade: true
priority_min_tier: {critical: 3}
features:
  legal-review: {min_tier: 3, budget_downgrade: false, reason: legal}
  autocomplete: {max_tier: 1}
  pinned-feature: {pin_model: mock-large}
  needs-tools: {requires: [tools]}
classifier:
  long_context_tokens: 6000
  keywords:
    1: [extract, format]
    2: [summarize, classify]
    3: [analyze, debug]
  risk_keywords: [contract, medical]
  structured_output_keywords: [json]
"""


@pytest.fixture
def registry():
    return ModelRegistry("config/models.yaml")


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "routing.yaml"
    path.write_text(ROUTING_YAML, encoding="utf-8")
    return str(path)


@pytest.fixture
def make_router(registry, config_path):
    def _make(profile: str = "test", available=("mock",)) -> Router:
        return Router(registry, load_routing_config(config_path, profile, registry),
                      set(available))
    return _make


def req(text: str, **kw) -> ChatRequest:
    kw.setdefault("team_id", "t")
    kw.setdefault("feature", "general")
    return ChatRequest(messages=[Message(role="user", content=text)], **kw)


def test_simple_prompt_routes_to_tier_1(make_router):
    d = make_router().route(req("Hello!"))
    assert (d.model, d.tier, d.source) == ("mock-echo", 1, "routed")


def test_complex_prompt_routes_to_tier_3_with_cheaper_fallbacks(make_router):
    d = make_router().route(req("Please analyze this design."))
    assert d.model == "mock-large" and d.tier == 3
    assert d.fallbacks == ("mock-medium", "mock-echo")


def test_risk_keyword_routes_to_tier_3(make_router):
    assert make_router().route(req("Review this contract")).tier == 3


def test_explicit_model_is_honoured(make_router):
    d = make_router().route(req("Hello!", model="mock-medium"))
    assert (d.model, d.source, d.fallbacks) == ("mock-medium", "explicit", ())


def test_unknown_explicit_model_raises(make_router):
    with pytest.raises(UnknownModelError):
        make_router().route(req("Hello!", model="nope"))


def test_feature_min_tier_and_no_downgrade(make_router):
    d = make_router().route(req("Hello!", feature="legal-review"))
    assert d.tier == 3 and d.fallbacks == ()


def test_feature_max_tier_clamps_classifier(make_router):
    d = make_router().route(req("Please analyze this.", feature="autocomplete"))
    assert d.tier == 1 and d.model == "mock-echo"
    assert any("clamped" in r for r in d.reasons)


def test_pinned_feature(make_router):
    d = make_router().route(req("Hello!", feature="pinned-feature"))
    assert (d.model, d.source) == ("mock-large", "pinned")


def test_priority_floor(make_router):
    d = make_router().route(req("Hello!", priority="critical"))
    assert d.tier == 3 and d.min_tier == 3 and d.fallbacks == ()


def test_unavailable_providers_are_skipped(make_router):
    assert make_router("keyed", {"mock"}).route(req("Hello!")).model == "mock-echo"
    assert make_router("keyed", {"mock", "openai"}).route(req("Hello!")).model == "gpt-4o-mini"


def test_required_capabilities(make_router):
    with pytest.raises(NoRouteError):
        make_router().route(req("Hello!", feature="needs-tools"))       # mocks lack tools
    d = make_router("keyed", {"mock", "openai"}).route(req("Hello!", feature="needs-tools"))
    assert d.model == "gpt-4o-mini"


def test_context_too_long_for_every_candidate(make_router):
    with pytest.raises(ContextTooLongError):
        make_router().route(req("x" * 200_000))


def test_baseline_model(make_router):
    assert make_router().baseline.name == "mock-large"            # defaults to tier 3's first
    assert make_router("keyed").baseline.name == "claude-sonnet-5-5"


@pytest.mark.parametrize("bad_yaml, message", [
    ("profiles: {test: {tiers: {1: [nope], 2: [mock-medium], 3: [mock-large]}}}", "nope"),
    ("profiles: {test: {tiers: {1: [mock-echo], 2: [mock-medium]}}}", "tier 3"),
    ("profiles: {test: {tiers: {1: [mock-echo], 2: [mock-medium], 3: [mock-large]}}}\n"
     "features: {bad: {min_tier: 3, max_tier: 1}}", "min_tier"),
    ("profiles: {test: {tiers: {1: [mock-echo], 2: [mock-medium], 3: [mock-large]}}}\n"
     "priority_min_tier: {urgent: 3}", "priority_min_tier"),
])
def test_invalid_config_fails_fast(registry, tmp_path, bad_yaml, message):
    path = tmp_path / "bad.yaml"
    path.write_text(bad_yaml, encoding="utf-8")
    with pytest.raises(RoutingConfigError, match=message):
        load_routing_config(str(path), "test", registry)


def test_missing_profile_fails_fast(registry, config_path):
    with pytest.raises(RoutingConfigError, match="not found"):
        load_routing_config(config_path, "nope", registry)