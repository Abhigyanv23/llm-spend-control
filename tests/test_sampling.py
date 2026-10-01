"""Deterministic hash-based sampling."""
import math
import uuid

import pytest

from app.quality import decide_sampling, hash_fraction
from app.quality.config import SamplingConfig

CFG = SamplingConfig(enabled=True, base_rate=0.10, low_confidence_rate=0.50,
                     low_confidence_threshold=0.6, skip_sources=("explicit", "pinned", "default"),
                     skip_top_tier=True)


def decide(request_id="r", source="routed", tier=1, confidence=0.85, **kw):
    return decide_sampling(CFG, request_id=request_id, source=source, final_tier=tier,
                           confidence=confidence, **kw)


def test_hash_fraction_is_deterministic_and_in_range():
    assert hash_fraction("abc") == hash_fraction("abc")
    assert hash_fraction("abc") != hash_fraction("abd")
    values = [hash_fraction(str(i)) for i in range(1000)]
    assert all(0.0 <= v < 1.0 for v in values)


def test_same_request_always_gets_the_same_decision():
    rid = str(uuid.uuid4())
    assert decide(rid) == decide(rid)


@pytest.mark.parametrize("confidence, expected_rate", [(0.85, 0.10), (0.5, 0.50)])
def test_sampled_fraction_converges_to_the_rate(confidence, expected_rate):
    n = 20_000
    hits = sum(decide(f"req-{i}", confidence=confidence).sampled for i in range(n))
    # 4 standard errors of a binomial proportion: fails by chance ~1 in 15,000 runs
    tolerance = 4 * math.sqrt(expected_rate * (1 - expected_rate) / n)
    assert abs(hits / n - expected_rate) < tolerance


def test_low_confidence_uses_the_higher_rate():
    assert decide(confidence=0.5).rate == 0.50
    assert decide(confidence=0.6).rate == 0.10           # threshold is strict "<"
    assert decide(confidence=None).rate == 0.10          # unknown confidence -> base rate


@pytest.mark.parametrize("kwargs, reason", [
    ({"source": "explicit"}, "explicit"),
    ({"source": "pinned"}, "pinned"),
    ({"tier": 3}, "top tier"),
    ({"escalated": True}, "escalated"),
    ({"verify_enabled": False}, "disabled"),
])
def test_ineligible_requests_are_never_sampled(kwargs, reason):
    for i in range(200):
        decision = decide(f"r{i}", **kwargs)
        assert not decision.sampled and reason in decision.reason


def test_rate_extremes():
    never = SamplingConfig(**{**CFG.__dict__, "base_rate": 0.0, "low_confidence_rate": 0.0})
    always = SamplingConfig(**{**CFG.__dict__, "base_rate": 1.0, "low_confidence_rate": 1.0})
    for i in range(200):
        args = dict(request_id=f"r{i}", source="routed", final_tier=1, confidence=0.9)
        assert not decide_sampling(never, **args).sampled
        assert decide_sampling(always, **args).sampled
