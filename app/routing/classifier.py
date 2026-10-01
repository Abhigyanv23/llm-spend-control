"""Request complexity classifier, rules-v1.

Turns a request into features (keywords, risk terms, code, size) and maps them to a tier with
a confidence score and human-readable reasons. Deliberately simple and explainable: it solves
the cold-start problem until Phase 4's verifier produces labels for a learned classifier.
Anything with the same classify() signature can replace it (Strategy pattern).
"""
import re
from dataclasses import dataclass

from app.routing.config import TIERS, ClassifierConfig
from app.schemas import ChatRequest
from app.tokens import estimate_input_tokens

# Code fences, or lines that start like code. Case-sensitive on purpose: "select the best"
# in prose must not count as SQL.
CODE_PATTERN = re.compile(r"```|^\s*(?:def|class|import|function|SELECT)\b|^\s*#include",
                          re.MULTILINE)

# rules-v2 (Phase 6), designed from TRAINING-split misses only:
# a negation cue up to two words before a keyword cancels it ("don't analyze", "no need to explain")
NEGATION_BEFORE = re.compile(r"\b(?:don'?t|do\s+not|does\s+not|no\s+need\s+to|without|not|never|"
                             r"skip)\b(?:\s+\w+){0,2}\s*$", re.IGNORECASE)
# open reasoning questions with no task keyword ("why would...", "what could be going on?")
REASONING_QUESTION = re.compile(r"(?:^|[.?!]\s+)(?:why\b|how\s+(?:would|could|will|does|do)\b|"
                                r"what\s+(?:happens|could|would|if)\b)", re.IGNORECASE)


def _compile(keywords: tuple[str, ...]) -> re.Pattern | None:
    if not keywords:
        return None
    # Longest first so "fix typos" wins over a shorter overlapping keyword
    alternatives = "|".join(re.escape(k) for k in sorted(set(keywords), key=len, reverse=True))
    # Whole words only ("format" must not match "formation"); optional plural suffix
    return re.compile(rf"\b(?:{alternatives})(?:s|es)?\b", re.IGNORECASE)


def _matches(pattern: re.Pattern | None, text: str) -> tuple[str, ...]:
    if pattern is None:
        return ()
    found: list[str] = []
    for match in pattern.finditer(text):
        word = match.group(0).lower()
        if word not in found:
            found.append(word)
    return tuple(found)


@dataclass(frozen=True)
class RequestFeatures:
    input_tokens: int
    message_count: int
    has_code: bool
    structured_output: bool
    keyword_hits: dict[int, tuple[str, ...]]
    risk_hits: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "message_count": self.message_count,
                "has_code": self.has_code, "structured_output": self.structured_output,
                "keyword_hits": {str(t): list(h) for t, h in self.keyword_hits.items() if h},
                "risk_hits": list(self.risk_hits)}


@dataclass(frozen=True)
class Classification:
    tier: int
    confidence: float           # 0..1: how sure the classifier is (low = verify first in Phase 4)
    reasons: tuple[str, ...]
    features: RequestFeatures
    classifier: str = "rules-v1"

    def to_dict(self) -> dict:
        return {"name": self.classifier, "tier": self.tier, "confidence": self.confidence,
                "reasons": list(self.reasons), "features": self.features.to_dict()}


def task_part(text: str) -> str:
    """rules-v2: the instruction without its payload. In "List the names in: Bob met Ann to
    review the plan", everything after the colon is DATA; its words describe the input, not the
    task. Only applied when the colon closes a multi-word instruction near the start (a one-word
    label like "Plan:" is not an instruction)."""
    first_line_break = text.find("\n")
    colon = text.find(":")
    if 0 < colon <= 160 and (first_line_break == -1 or colon < first_line_break or
                             text[colon + 1:colon + 2] == "\n"):
        head = text[:colon]
        if len(head.split()) >= 2:
            return head
    return text


def _matches_v2(pattern: re.Pattern | None, text: str) -> tuple[str, ...]:
    """Like _matches, but drops keywords negated by a cue just before them."""
    if pattern is None:
        return ()
    found: list[str] = []
    for match in pattern.finditer(text):
        if NEGATION_BEFORE.search(text[max(0, match.start() - 40):match.start()]):
            continue
        word = match.group(0).lower()
        if word not in found:
            found.append(word)
    return tuple(found)


class RuleBasedClassifier:
    name = "rules-v1"

    def __init__(self, config: ClassifierConfig, version: str | None = None):
        self.config = config
        self.version = version or config.version
        self.name = self.version
        self._tier_patterns = {t: _compile(config.keywords.get(t, ())) for t in TIERS}
        self._risk = _compile(config.risk_keywords)
        self._structured = _compile(config.structured_output_keywords)

    @staticmethod
    def instruction_text(request: ChatRequest) -> str:
        """System prompts + the latest user message: the actual task. Earlier turns and
        assistant replies are context, and their keywords would add noise."""
        system = [m.content for m in request.messages if m.role == "system"]
        last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        return "\n".join([*system, last_user])

    def extract(self, request: ChatRequest) -> RequestFeatures:
        instructions = self.instruction_text(request)
        everything = "\n".join(m.content for m in request.messages)
        if self.version == "rules-v2":
            # Task keywords: instruction part only, negations removed. Risk keywords still scan
            # the full text: missing a risky request is the costlier error.
            task = task_part(instructions)
            hits = {t: _matches_v2(p, task) for t, p in self._tier_patterns.items()}
            if REASONING_QUESTION.search(task) and not hits[3]:
                hits[3] = ("reasoning question",)
        else:
            hits = {t: _matches(p, instructions) for t, p in self._tier_patterns.items()}
        return RequestFeatures(
            input_tokens=estimate_input_tokens(request.messages),
            message_count=len(request.messages),
            has_code=bool(CODE_PATTERN.search(everything)),
            structured_output=bool(_matches(self._structured, instructions)),
            keyword_hits=hits,
            risk_hits=_matches(self._risk, instructions),
        )

    def classify(self, request: ChatRequest) -> Classification:
        f = self.extract(request)
        reasons: list[str] = []
        hit_tiers = sorted(t for t, hits in f.keyword_hits.items() if hits)

        # 1. Base tier from the strongest signal
        if f.risk_hits:
            tier = 3
            reasons.append(f"risk keywords: {', '.join(f.risk_hits)}")
        elif hit_tiers:
            tier = hit_tiers[-1]
            reasons.append(f"tier-{tier} keywords: {', '.join(f.keyword_hits[tier])}")
        else:
            # Optimistic default: cheapest tier, low confidence (Phase 4 verifies these first)
            tier = 1
            reasons.append("no complexity signals: defaulting to the cheapest tier")

        # 2. Floors from structural signals
        if f.has_code and tier < 2:
            tier = 2
            reasons.append("contains code: at least tier 2")
        if f.input_tokens > self.config.long_context_tokens and tier < 2:
            tier = 2
            reasons.append(f"large input (~{f.input_tokens} tokens): at least tier 2")
        if f.structured_output and tier == 1:
            reasons.append("structured output requested (suits a tier-1 model)")

        # 3. Confidence: clear single signal > mixed signals > no signal
        if f.risk_hits:
            confidence = 0.9
        elif not hit_tiers:
            confidence = 0.5
        elif len(hit_tiers) == 1:
            confidence = 0.85
        else:
            confidence = 0.65
            reasons.append(f"mixed signals across tiers {hit_tiers}: took the highest")

        return Classification(tier=tier, confidence=confidence, reasons=tuple(reasons),
                              features=f, classifier=self.version)
