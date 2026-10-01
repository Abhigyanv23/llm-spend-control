"""Judges decide whether a cheap answer was good enough, compared with a reference answer.

Strategy pattern: one interface (`Judge.judge`), interchangeable implementations.
  SimilarityJudge  free and deterministic: for development, tests and the mock models
  LLMJudge         a strong model grades the cheap answer against the reference: production
"""
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher

from app.money import usd_str
from app.providers.base import ProviderAdapter
from app.registry import ModelSpec
from app.schemas import ChatRequest, Message
from app.tokens import estimate_input_tokens

VERDICTS = ("pass", "fail", "inconclusive")
MOCK_TAG = re.compile(r"^\s*\[mock:[^\]]*\]\s*")
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class JudgeResult:
    verdict: str                    # pass | fail | inconclusive
    score: float | None             # 0..1, None if unknown
    reason: str
    judge: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "score": self.score, "reason": self.reason,
                "judge": self.judge, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cost_usd": usd_str(self.cost_usd),
                **({"metadata": self.metadata} if self.metadata else {})}


class Judge(ABC):
    name: str = "judge"

    @abstractmethod
    async def judge(self, *, prompt: str, candidate: str, reference: str) -> JudgeResult:
        """Grade `candidate` (the cheap answer) for `prompt`, using `reference` as the
        stronger model's answer."""

    def worst_case_cost(self, *, prompt: str, candidate: str, reference: str) -> Decimal:
        """Upper bound of what judging costs, for budget reservation. Free by default."""
        return Decimal(0)


# ------------------------------------------------------------------ similarity judge

def normalise(text: str) -> str:
    """Remove the mock model tag, lower-case and collapse whitespace, so two models that
    say the same thing compare as equal."""
    return WHITESPACE.sub(" ", MOCK_TAG.sub("", text or "")).strip().lower()


def similarity(a: str, b: str, method: str = "sequence") -> float:
    words_a, words_b = normalise(a).split(), normalise(b).split()
    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0
    if method == "jaccard":
        set_a, set_b = set(words_a), set(words_b)
        return len(set_a & set_b) / len(set_a | set_b)
    # autojunk=False: by default difflib ignores "popular" items in sequences of 200+ elements,
    # which gives nonsense scores for natural language
    return SequenceMatcher(None, words_a, words_b, autojunk=False).ratio()


class SimilarityJudge(Judge):
    def __init__(self, threshold: float = 0.8, method: str = "sequence"):
        self.threshold = threshold
        self.method = method
        self.name = f"similarity-{method}"

    async def judge(self, *, prompt: str, candidate: str, reference: str) -> JudgeResult:
        score = round(similarity(candidate, reference, self.method), 4)
        verdict = "pass" if score >= self.threshold else "fail"
        return JudgeResult(verdict=verdict, score=score, judge=self.name,
                           reason=f"{self.method} similarity {score:.3f} "
                                  f"{'>=' if verdict == 'pass' else '<'} threshold "
                                  f"{self.threshold}")


# ------------------------------------------------------------------ LLM judge

JUDGE_SYSTEM_PROMPT = """You are a strict, impartial grader of AI assistant answers.
You receive a QUESTION, a REFERENCE answer written by a strong model, and a CANDIDATE answer.
Decide whether the CANDIDATE could be given to the user instead of the REFERENCE.

Rules:
- Judge substance: correctness, completeness, and following the question's instructions
  (including any required format such as JSON).
- Do NOT reward length or style. A shorter candidate that covers the same facts passes.
- The reference can be imperfect; if the candidate is correct where the reference is not, pass it.
- An empty answer, a refusal, or a cut-off answer fails.

Respond with ONLY one JSON object and nothing else:
{"verdict": "pass" or "fail", "score": <number from 0 to 1>, "reason": "<one short sentence>"}"""


@dataclass(frozen=True)
class ParsedJudgement:
    verdict: str
    score: float | None
    reason: str
    metadata: dict = field(default_factory=dict)


FENCED = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _inconclusive(reason: str, raw: str) -> ParsedJudgement:
    return ParsedJudgement("inconclusive", None, reason, {"raw_output": (raw or "")[:300]})


def parse_judge_output(text: str) -> ParsedJudgement:
    """Parse the judge's JSON robustly. Anything unusable is 'inconclusive', never a crash:
    a judge that rambles must not take the worker down or record a false pass/fail."""
    if not text or not text.strip():
        return _inconclusive("judge returned an empty response", text)

    data = None
    # Prefer fenced ```json blocks; then the outermost {...} in the whole text
    for chunk in [m.group(1) for m in FENCED.finditer(text)] + [text]:
        start, end = chunk.find("{"), chunk.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            candidate = json.loads(chunk[start:end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            data = candidate
            break
    if data is None:
        return _inconclusive("judge output is not valid JSON", text)

    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("pass", "fail"):
        return _inconclusive(f"invalid verdict {data.get('verdict')!r}", text)

    score = data.get("score")
    if score is not None:
        if isinstance(score, bool):
            return _inconclusive("score must be a number, got a boolean", text)
        try:
            score = float(score)
        except (TypeError, ValueError):
            return _inconclusive(f"score {data.get('score')!r} is not a number", text)
        if not 0.0 <= score <= 1.0:
            return _inconclusive(f"score {score} is outside 0..1", text)

    metadata = {}
    # Calibration check: a "pass" with a low score (or the reverse) is a sign of a confused
    # judge. Keep the verdict, but flag it so these cases can be reviewed.
    if score is not None and ((verdict == "pass") != (score >= 0.5)):
        metadata["inconsistent"] = True
    return ParsedJudgement(verdict, score, str(data.get("reason") or "")[:500], metadata)


class LLMJudge(Judge):
    """Asks a strong model to grade the candidate against the reference.
    Provider errors are NOT caught here: they are transient, and the worker retries the job."""

    def __init__(self, adapter: ProviderAdapter, spec: ModelSpec, *, team_id: str,
                 feature: str, max_tokens: int = 300, max_chars: int = 6000):
        self.adapter = adapter
        self.spec = spec
        self.team_id = team_id
        self.feature = feature
        self.max_tokens = max_tokens
        self.max_chars = max_chars      # cap each section so one huge prompt can't blow the budget
        self.name = f"llm:{spec.name}"

    def build_request(self, *, prompt: str, candidate: str, reference: str) -> ChatRequest:
        cap = self.max_chars
        body = (f"QUESTION:\n{prompt[:cap]}\n\n"
                f"REFERENCE ANSWER:\n{reference[:cap]}\n\n"
                f"CANDIDATE ANSWER:\n{candidate[:cap]}")
        return ChatRequest(team_id=self.team_id, feature=self.feature,
                           max_tokens=self.max_tokens, temperature=0.0,
                           messages=[Message(role="system", content=JUDGE_SYSTEM_PROMPT),
                                     Message(role="user", content=body)])

    def worst_case_cost(self, *, prompt: str, candidate: str, reference: str) -> Decimal:
        request = self.build_request(prompt=prompt, candidate=candidate, reference=reference)
        return self.spec.worst_case_cost(estimate_input_tokens(request.messages), self.max_tokens)

    async def judge(self, *, prompt: str, candidate: str, reference: str) -> JudgeResult:
        request = self.build_request(prompt=prompt, candidate=candidate, reference=reference)
        result = await self.adapter.complete(request, self.spec.name)
        parsed = parse_judge_output(result.output)
        return JudgeResult(verdict=parsed.verdict, score=parsed.score, reason=parsed.reason,
                           judge=self.name, input_tokens=result.input_tokens,
                           output_tokens=result.output_tokens,
                           cost_usd=self.spec.cost(result.input_tokens, result.output_tokens),
                           metadata=parsed.metadata)


# ------------------------------------------------------------------ factory

def build_judge(cfg, registry, adapters: dict[str, ProviderAdapter], *, team_id: str,
                feature: str) -> Judge:
    """Pick the judge strategy from quality.yaml's verification.judge section."""
    if cfg.type == "llm":
        spec = registry.get(cfg.model)
        return LLMJudge(adapters[spec.provider], spec, team_id=team_id, feature=feature,
                        max_tokens=cfg.max_tokens)
    return SimilarityJudge(cfg.similarity_threshold, cfg.similarity_method)
