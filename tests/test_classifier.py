from app.routing import ClassifierConfig, RuleBasedClassifier
from app.schemas import ChatRequest, Message

CONFIG = ClassifierConfig(
    keywords={1: ("extract", "format"), 2: ("summarize", "classify"),
              3: ("analyze", "debug", "step by step")},
    risk_keywords=("contract", "medical"),
    structured_output_keywords=("json",),
    long_context_tokens=6000,
)
clf = RuleBasedClassifier(CONFIG)


def req(*messages: tuple[str, str], **kw) -> ChatRequest:
    return ChatRequest(messages=[Message(role=r, content=c) for r, c in messages],
                       team_id="t", feature="f", **kw)


def user(text: str, **kw) -> ChatRequest:
    return req(("user", text), **kw)


def test_no_signals_defaults_to_tier_1_with_low_confidence():
    c = clf.classify(user("Hello there!"))
    assert c.tier == 1 and c.confidence == 0.5


def test_single_tier_keyword():
    c = clf.classify(user("Please summarize this article."))
    assert c.tier == 2 and c.confidence == 0.85
    assert "summarize" in c.features.keyword_hits[2]


def test_mixed_signals_take_highest_tier_with_lower_confidence():
    c = clf.classify(user("Extract the numbers, then analyze the trend."))
    assert c.tier == 3 and c.confidence == 0.65


def test_risk_keywords_force_tier_3():
    c = clf.classify(user("Format this contract clause nicely."))
    assert c.tier == 3 and c.confidence == 0.9
    assert c.features.risk_hits == ("contract",)


def test_code_forces_at_least_tier_2():
    c = clf.classify(user("```python\nprint('hi')\n```"))
    assert c.tier == 2 and c.features.has_code


def test_long_input_forces_at_least_tier_2():
    c = clf.classify(user("word " * 30_000))
    assert c.tier == 2


def test_whole_word_matching_and_plurals():
    assert clf.classify(user("The formation of stars")).tier == 1          # not "format"
    assert "summarizes" in clf.classify(user("It summarizes")).features.keyword_hits[2]
    assert not clf.classify(user("Please classify these")).features.has_code   # not "class"


def test_only_system_prompt_and_last_user_message_are_instructions():
    c = clf.classify(req(("user", "Analyze everything deeply"),
                         ("assistant", "Done."),
                         ("user", "Thanks!")))
    assert c.tier == 1      # the old "analyze" turn is context, not the current task
    c = clf.classify(req(("system", "You debug Python code."), ("user", "Thanks!")))
    assert c.tier == 3


def test_structured_output_flag():
    c = clf.classify(user("Return the result as JSON"))
    assert c.features.structured_output