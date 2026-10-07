"""rules-v2: payload exclusion, negation, reasoning questions; version selection."""
from pathlib import Path

import pytest

from app.registry import ModelRegistry
from app.routing import RoutingConfigError, RuleBasedClassifier, load_routing_config
from app.routing.classifier import task_part
from app.schemas import ChatRequest, Message

REGISTRY = ModelRegistry("config/models.yaml")
CONFIG = load_routing_config("config/routing.yaml", "dev", REGISTRY).classifier
V1, V2 = RuleBasedClassifier(CONFIG, "rules-v1"), RuleBasedClassifier(CONFIG, "rules-v2")


def tier(clf, text: str) -> int:
    return clf.classify(ChatRequest(team_id="t", feature="f",
                                    messages=[Message(role="user", content=text)])).tier


@pytest.mark.parametrize("text", [
    "Don't analyze anything, just list the dates in: the 3rd and the 9th",
    "No need to explain why, just extract the email from: a@b.com",
    "Do not compare them, simply count the items: a, b, c",
])
def test_negated_keywords_are_ignored(text):
    assert tier(V1, text) == 3 and tier(V2, text) == 1


def test_keywords_in_the_payload_are_data_not_instructions():
    text = "List the person names mentioned here: Bob met Ann to review the plan."
    assert task_part(text) == "List the person names mentioned here"
    assert tier(V1, text) == 3 and tier(V2, text) == 1


def test_one_word_label_is_not_treated_as_an_instruction():
    assert task_part("Plan: sort these") == "Plan: sort these"


def test_open_reasoning_questions_go_to_tier_3():
    text = "Why would increasing the cache size make p99 latency worse?"
    assert tier(V1, text) == 1 and tier(V2, text) == 3
    assert tier(V2, "Our error rate doubles on Mondays. What could be going on?") == 3


def test_v2_keeps_v1_behaviour_on_ordinary_prompts():
    for text, expected in [("Summarize this meeting transcript", 2),
                           ("Analyze the trade-offs of this design", 3), ("Hello!", 1),
                           ("Review this contract clause: the supplier is liable", 3)]:
        assert tier(V1, text) == tier(V2, text) == expected


def test_risk_keywords_still_scan_the_payload():
    # A risky request stays tier 3 even when the risk word sits after the colon
    assert tier(V2, "Check this text for me: the dosage was doubled") == 3


def test_version_is_reported_and_validated(tmp_path):
    result = V2.classify(ChatRequest(team_id="t", feature="f",
                                     messages=[Message(role="user", content="hi")]))
    assert result.classifier == "rules-v2"
    bad = tmp_path / "routing.yaml"
    bad.write_text(Path("config/routing.yaml").read_text(encoding="utf-8").replace(
        "version: rules-v", "version: rules-v9-"), encoding="utf-8")
    with pytest.raises(RoutingConfigError, match="classifier.version"):
        load_routing_config(str(bad), "dev", REGISTRY)
