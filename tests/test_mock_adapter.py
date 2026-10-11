"""Mock capability limits and test directives (Phase 4)."""
import json

import pytest

from app.providers.mock_adapter import REFUSAL_TEXT, MockAdapter
from app.schemas import ChatRequest, Message

MOCKS = ("mock-echo", "mock-medium", "mock-large")


def request(text: str, max_tokens: int = 512) -> ChatRequest:
    return ChatRequest(team_id="t", feature="f", max_tokens=max_tokens,
                       messages=[Message(role="user", content=text)])


async def complete(model: str, text: str, max_tokens: int = 512):
    return await MockAdapter().complete(request(text, max_tokens), model)


@pytest.mark.parametrize("model", MOCKS)
async def test_short_prompts_are_unchanged_from_phase_3(model):
    result = await complete(model, "Hello gateway", 1000)
    assert result.output == f"[mock:{model}] You said: Hello gateway"
    assert result.output_tokens == len(result.output) // 4
    assert result.metadata == {"finish_reason": "stop"}


@pytest.mark.parametrize("model, chars", [("mock-echo", 200), ("mock-medium", 1000),
                                          ("mock-large", 4000)])
async def test_capability_limits_by_model(model, chars):
    text = "word " * 2000                                    # 10,000 chars
    result = await complete(model, text, 8192)
    assert result.output == f"[mock:{model}] You said: {text[:chars]}"


@pytest.mark.parametrize("directive, check", [
    ("empty", lambda r: r.output == "" and r.output_tokens == 0),
    ("refuse", lambda r: r.output == REFUSAL_TEXT),
    ("truncate", lambda r: r.metadata["finish_reason"] == "length" and r.output_tokens == 512),
    ("badjson", lambda r: r.output.startswith('{"answer": "')),
])
async def test_directives_on_tier1_mock(directive, check):
    result = await complete("mock-echo", f"Summarise this [[mock:{directive}]] please")
    assert check(result)
    assert result.metadata["mock_directive"] == directive


async def test_badjson_really_is_invalid_json():
    result = await complete("mock-echo", "Return JSON [[mock:badjson]]")
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


@pytest.mark.parametrize("model", ["mock-medium", "mock-large"])
async def test_stronger_mocks_ignore_directives(model):
    result = await complete(model, "Answer this [[mock:empty]]")
    assert result.output == f"[mock:{model}] You said: Answer this [[mock:empty]]"
    assert "mock_directive" not in result.metadata
