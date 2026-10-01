"""config/quality.yaml loading and fail-fast validation."""
import copy

import pytest
import yaml

from app.quality import QualityConfigError, load_quality_config
from app.registry import ModelRegistry
from app.routing import load_routing_config

REGISTRY = ModelRegistry("config/models.yaml")
DEV = load_routing_config("config/routing.yaml", "dev", REGISTRY)
PROD = load_routing_config("config/routing.yaml", "production", REGISTRY)
KEYLESS = {"mock", "ollama"}
with open("config/quality.yaml", encoding="utf-8") as f:
    BASE = yaml.safe_load(f)


def load_variant(tmp_path, mutate, routing=DEV, available=KEYLESS, verify_enabled=True):
    data = copy.deepcopy(BASE)
    mutate(data)
    path = tmp_path / "quality.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_quality_config(str(path), REGISTRY, routing, available, verify_enabled)


def test_default_config_loads_for_dev():
    cfg = load_quality_config("config/quality.yaml", REGISTRY, DEV, KEYLESS)
    assert cfg.verification.reference_model == "mock-large"      # first usable tier-3 candidate
    assert cfg.verification.judge.type == "similarity"
    assert cfg.sampling.base_rate == 0.10 and cfg.sampling.low_confidence_rate == 0.50
    assert cfg.escalation.post_call_checks == ("empty", "refusal", "truncated", "invalid_json")
    assert cfg.escalation.max_escalations == 1
    assert "i'm sorry, but i can't" in cfg.escalation.refusal_patterns


def test_production_without_keys_fails_fast():
    with pytest.raises(QualityConfigError, match="No usable reference model"):
        load_quality_config("config/quality.yaml", REGISTRY, PROD, KEYLESS)


def test_production_with_anthropic_key_uses_sonnet():
    cfg = load_quality_config("config/quality.yaml", REGISTRY, PROD, KEYLESS | {"anthropic"})
    assert cfg.verification.reference_model == "claude-sonnet-5-5"


def test_verification_disabled_needs_no_reference_model():
    cfg = load_quality_config("config/quality.yaml", REGISTRY, PROD, KEYLESS,
                              verify_enabled=False)
    assert cfg.verify_enabled is False and cfg.verification.reference_model is None


def test_llm_judge_defaults_to_reference_model(tmp_path):
    cfg = load_variant(tmp_path, lambda d: d["verification"]["judge"].update(type="llm"))
    assert cfg.verification.judge.model == "mock-large"


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["sampling"].update(base_rate=1.5), "base_rate"),
    (lambda d: d["sampling"].update(base_rate=0.5, low_confidence_rate=0.1), "MORE checking"),
    (lambda d: d["sampling"].update(skip_sources=["robots"]), "skip_sources"),
    (lambda d: d["verification"]["judge"].update(type="vibes"), "judge.type"),
    (lambda d: d["verification"]["judge"].update(similarity_threshold=0), "similarity_threshold"),
    (lambda d: d["verification"].update(reference_model="gpt-99"), "not in the model registry"),
    (lambda d: d["verification"]["budget"].update(team_id="has spaces"), "team_id"),
    (lambda d: d["verification"].update(max_attempts=0), "max_attempts"),
    (lambda d: d["verification"].update(dead_letter_stream="quality:verify"), "different"),
    (lambda d: d["escalation"]["pre_call"].update(priorities=["urgent"]), "priorities"),
    (lambda d: d["escalation"]["post_call"].update(checks=["vibes"]), "checks"),
    (lambda d: d["escalation"].update(max_escalations=5), "max_escalations"),
    (lambda d: d["privacy"].update(max_prompt_chars=0), "max_prompt_chars"),
])
def test_invalid_values_fail_fast(tmp_path, mutate, message):
    with pytest.raises(QualityConfigError, match=message):
        load_variant(tmp_path, mutate)
