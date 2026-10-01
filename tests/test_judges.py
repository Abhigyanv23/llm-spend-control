"""SimilarityJudge, LLMJudge and robust judge-output parsing."""
from decimal import Decimal

import pytest

from app.errors import ProviderError
from app.providers.base import ProviderAdapter, ProviderResult
from app.providers.mock_adapter import MockAdapter
from app.quality import LLMJudge, SimilarityJudge, parse_judge_output, similarity
from app.quality.judges import normalise
from app.registry import ModelRegistry
from app.schemas import ChatRequest, Message

REGISTRY = ModelRegistry("config/models.yaml")


# ------------------------------------------------------------------ similarity

def test_normalise_strips_mock_tag_case_and_whitespace():
    assert normalise("[mock:mock-echo] You  said:\n Hi") == "you said: hi"


async def test_identical_answers_from_different_mocks_pass():
    judge = SimilarityJudge(0.8)
    result = await judge.judge(prompt="hi", candidate="[mock:mock-echo] You said: hello there",
                               reference="[mock:mock-large] You said: hello there")
    assert (result.verdict, result.score) == ("pass", 1.0)
    assert result.cost_usd == 0


async def test_truncated_cheap_answer_fails_against_the_reference():
    """The realistic dev miss: a long prompt that mock-echo can only half-answer."""
    prompt = " ".join(f"point{i}" for i in range(300))      # ~2,100 chars
    adapter = MockAdapter()
    req = ChatRequest(team_id="t", feature="f", max_tokens=4000,
                      messages=[Message(role="user", content=prompt)])
    cheap = (await adapter.complete(req, "mock-echo")).output
    reference = (await adapter.complete(req, "mock-large")).output
    result = await SimilarityJudge(0.8).judge(prompt=prompt, candidate=cheap,
                                              reference=reference)
    assert result.verdict == "fail" and result.score < 0.3


def test_jaccard_ignores_order_sequence_does_not():
    a, b = "alpha beta gamma delta", "delta gamma beta alpha"
    assert similarity(a, b, "jaccard") == 1.0
    assert similarity(a, b, "sequence") < 1.0


def test_empty_cases():
    assert similarity("", "") == 1.0
    assert similarity("", "something") == 0.0


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize("text, verdict, score", [
    ('{"verdict": "pass", "score": 0.9, "reason": "same facts"}', "pass", 0.9),
    ('Sure! ```json\n{"verdict": "FAIL", "score": 0.2, "reason": "x"}\n```', "fail", 0.2),
    ('Here you go: {"verdict": "pass", "score": "0.75", "reason": "ok"} hope it helps',
     "pass", 0.75),
    ('{"verdict": "pass", "reason": "no score given"}', "pass", None),
])
def test_parse_valid_outputs(text, verdict, score):
    parsed = parse_judge_output(text)
    assert (parsed.verdict, parsed.score) == (verdict, score)


@pytest.mark.parametrize("text, reason_part", [
    ("", "empty"),
    ("The candidate looks fine to me.", "not valid JSON"),
    ('{"verdict": "pass", "score": 0.9', "not valid JSON"),
    ('{"verdict": "maybe", "score": 0.5}', "invalid verdict"),
    ('{"verdict": "pass", "score": 1.7}', "outside 0..1"),
    ('{"verdict": "pass", "score": "high"}', "not a number"),
    ('{"verdict": "pass", "score": true}', "boolean"),
    ('["pass", 0.9]', "not valid JSON"),
])
def test_unusable_outputs_are_inconclusive_never_a_crash(text, reason_part):
    parsed = parse_judge_output(text)
    assert parsed.verdict == "inconclusive" and parsed.score is None
    assert reason_part in parsed.reason


def test_inconsistent_verdict_and_score_is_flagged():
    assert parse_judge_output('{"verdict": "pass", "score": 0.1}').metadata == {
        "inconsistent": True}
    assert parse_judge_output('{"verdict": "fail", "score": 0.1}').metadata == {}


# ------------------------------------------------------------------ LLM judge

class ScriptedAdapter(ProviderAdapter):
    """Returns a fixed output and records the request it was given."""
    name = "scripted"

    def __init__(self, output: str = "", error: Exception | None = None):
        self.output, self.error, self.requests = output, error, []

    async def complete(self, request, model):
        self.requests.append(request)
        if self.error:
            raise self.error
        return ProviderResult(output=self.output, input_tokens=1000, output_tokens=50,
                              raw_model=model)


def llm_judge(adapter) -> LLMJudge:
    return LLMJudge(adapter, REGISTRY.get("claude-sonnet-5-5"), team_id="quality-verifier",
                    feature="verification")


async def test_llm_judge_pass_with_cost():
    adapter = ScriptedAdapter('{"verdict": "pass", "score": 0.92, "reason": "equivalent"}')
    result = await llm_judge(adapter).judge(prompt="Q", candidate="A", reference="B")
    assert (result.verdict, result.score, result.judge) == ("pass", 0.92,
                                                           "llm:claude-sonnet-5-5")
    # (1000 x $3 + 50 x $15) / 1e6, illustrative prices
    assert result.cost_usd == Decimal("0.00375000")
    request = adapter.requests[0]
    assert request.temperature == 0.0 and request.team_id == "quality-verifier"
    assert "REFERENCE ANSWER:\nB" in request.messages[1].content


async def test_llm_judge_garbage_is_inconclusive():
    result = await llm_judge(ScriptedAdapter("I think it's fine?")).judge(
        prompt="Q", candidate="A", reference="B")
    assert result.verdict == "inconclusive" and result.cost_usd > 0   # the call still cost money


async def test_llm_judge_provider_errors_propagate_for_retry():
    adapter = ScriptedAdapter(error=ProviderError("anthropic", "Upstream timeout", 504, True))
    with pytest.raises(ProviderError):
        await llm_judge(adapter).judge(prompt="Q", candidate="A", reference="B")


def test_llm_judge_worst_case_cost_is_bounded_by_section_caps():
    judge = llm_judge(ScriptedAdapter())
    huge = "x" * 1_000_000
    capped = judge.worst_case_cost(prompt=huge, candidate=huge, reference=huge)
    assert capped < Decimal("0.02")          # 3 x 6,000 chars ~ 4,500 tokens + 300 output
