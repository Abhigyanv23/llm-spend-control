"""Quality configuration (config/quality.yaml): sampling, verification, escalation, privacy.

Loaded once at startup and validated against the model registry and the active routing
profile, so a typo stops the server immediately instead of silently disabling quality checks.
"""
import re
from dataclasses import asdict, dataclass

import yaml

from app.errors import UnknownModelError
from app.registry import ModelRegistry
from app.routing.config import RoutingConfig
from app.schemas import ID_PATTERN, Priority

JUDGE_TYPES = ("similarity", "llm")
SIMILARITY_METHODS = ("sequence", "jaccard")
POST_CALL_CHECKS = ("empty", "refusal", "truncated", "invalid_json")
ROUTING_SOURCES = ("explicit", "pinned", "routed", "default")
AUTO = "auto"


class QualityConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SamplingConfig:
    enabled: bool
    base_rate: float
    low_confidence_rate: float
    low_confidence_threshold: float
    skip_sources: tuple[str, ...]
    skip_top_tier: bool


@dataclass(frozen=True)
class JudgeConfig:
    type: str                       # similarity | llm
    model: str | None               # resolved judge model (llm only)
    similarity_method: str
    similarity_threshold: float
    max_tokens: int


@dataclass(frozen=True)
class VerificationConfig:
    reference_model: str | None     # resolved; None only when verification is disabled
    reference_max_tokens: int
    judge: JudgeConfig
    budget_team_id: str
    budget_feature: str
    max_attempts: int
    job_timeout_s: float
    stream: str
    dead_letter_stream: str
    consumer_group: str
    maxlen: int


@dataclass(frozen=True)
class EscalationConfig:
    enabled: bool
    pre_call_priorities: tuple[str, ...]
    pre_call_confidence_below: float
    post_call_checks: tuple[str, ...]
    max_escalations: int
    refusal_patterns: tuple[str, ...]


@dataclass(frozen=True)
class PrivacyConfig:
    store_prompts: bool
    max_prompt_chars: int
    prompt_preview_chars: int = 120     # analytics preview in request_logs (0 = none)


@dataclass(frozen=True)
class QualityConfig:
    verify_enabled: bool
    sampling: SamplingConfig
    verification: VerificationConfig
    escalation: EscalationConfig
    privacy: PrivacyConfig

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ validation helpers

def _fraction(name: str, value, *, allow_zero: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise QualityConfigError(f"{name} must be a number, got {value!r}") from None
    low_ok = number >= 0 if allow_zero else number > 0
    if not (low_ok and number <= 1):
        raise QualityConfigError(f"{name} must be in {'[0' if allow_zero else '(0'}, 1], "
                                 f"got {number}")
    return number


def _positive_int(name: str, value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise QualityConfigError(f"{name} must be an integer, got {value!r}") from None
    if number < 1:
        raise QualityConfigError(f"{name} must be >= 1, got {number}")
    return number


def _non_negative_int(name: str, value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise QualityConfigError(f"{name} must be an integer, got {value!r}") from None
    if number < 0:
        raise QualityConfigError(f"{name} must be >= 0, got {number}")
    return number


def _one_of(name: str, value, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise QualityConfigError(f"{name} must be one of {list(allowed)}, got {value!r}")
    return value


def _identifier(name: str, value) -> str:
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERN, value) or len(value) > 64:
        raise QualityConfigError(f"{name} must match {ID_PATTERN} (max 64 chars), got {value!r}")
    return value


def _require_model(registry: ModelRegistry, name: str, where: str):
    try:
        return registry.get(name)
    except UnknownModelError:
        raise QualityConfigError(f"{where}: model '{name}' is not in the model registry") from None


def select_reference_model(routing: RoutingConfig, registry: ModelRegistry,
                           available: set[str]) -> str | None:
    """First tier-3 candidate of the active profile whose provider is available."""
    for name in routing.tiers[3]:
        if registry.get(name).provider in available:
            return name
    return None


def _resolve_reference(raw: str, routing: RoutingConfig, registry: ModelRegistry,
                       available: set[str], verify_enabled: bool) -> str | None:
    if raw == AUTO:
        name = select_reference_model(routing, registry, available)
    else:
        spec = _require_model(registry, raw, "verification.reference_model")
        name = spec.name if spec.provider in available else None
    if name is None and verify_enabled:
        raise QualityConfigError(
            f"No usable reference model for verification in routing profile "
            f"'{routing.profile}' (tier-3 candidates: {list(routing.tiers[3])}; available "
            f"providers: {sorted(available)}). Set an API key, choose another profile, or set "
            f"VERIFY_ENABLED=false.")
    return name


# ------------------------------------------------------------------ loader

def load_quality_config(path: str, registry: ModelRegistry, routing: RoutingConfig,
                        available: set[str], verify_enabled: bool = True) -> QualityConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # --- sampling
    raw = data.get("sampling") or {}
    base_rate = _fraction("sampling.base_rate", raw.get("base_rate", 0.1))
    low_rate = _fraction("sampling.low_confidence_rate", raw.get("low_confidence_rate", base_rate))
    if low_rate < base_rate:
        raise QualityConfigError("sampling.low_confidence_rate must be >= sampling.base_rate "
                                 "(uncertain decisions deserve MORE checking, not less)")
    skip_sources = tuple(raw.get("skip_sources") or ())
    for source in skip_sources:
        _one_of("sampling.skip_sources[]", source, ROUTING_SOURCES)
    sampling = SamplingConfig(
        enabled=bool(raw.get("enabled", True)), base_rate=base_rate, low_confidence_rate=low_rate,
        low_confidence_threshold=_fraction("sampling.low_confidence_threshold",
                                           raw.get("low_confidence_threshold", 0.6)),
        skip_sources=skip_sources, skip_top_tier=bool(raw.get("skip_top_tier", True)))

    # --- verification
    raw = data.get("verification") or {}
    reference = _resolve_reference(str(raw.get("reference_model", AUTO)), routing, registry,
                                   available, verify_enabled)
    raw_judge = raw.get("judge") or {}
    judge_type = _one_of("verification.judge.type", raw_judge.get("type", "similarity"),
                         JUDGE_TYPES)
    judge_model = None
    if judge_type == "llm":
        raw_model = str(raw_judge.get("model", AUTO))
        judge_model = reference if raw_model == AUTO else _require_model(
            registry, raw_model, "verification.judge.model").name
        if judge_model is None and verify_enabled:
            raise QualityConfigError("verification.judge.type is 'llm' but no judge model "
                                     "is usable")
    judge = JudgeConfig(
        type=judge_type, model=judge_model,
        similarity_method=_one_of("verification.judge.similarity_method",
                                  raw_judge.get("similarity_method", "sequence"),
                                  SIMILARITY_METHODS),
        similarity_threshold=_fraction("verification.judge.similarity_threshold",
                                       raw_judge.get("similarity_threshold", 0.8),
                                       allow_zero=False),
        max_tokens=_positive_int("verification.judge.max_tokens",
                                 raw_judge.get("max_tokens", 300)))
    raw_budget = raw.get("budget") or {}
    stream = str(raw.get("stream", "quality:verify"))
    dead_letter = str(raw.get("dead_letter_stream", f"{stream}:dead"))
    if not stream or stream == dead_letter:
        raise QualityConfigError("verification.stream and dead_letter_stream must be "
                                 "non-empty and different")
    job_timeout = float(raw.get("job_timeout_s", 60))
    if job_timeout <= 0:
        raise QualityConfigError("verification.job_timeout_s must be > 0")
    verification = VerificationConfig(
        reference_model=reference,
        reference_max_tokens=_positive_int("verification.reference_max_tokens",
                                           raw.get("reference_max_tokens", 1024)),
        judge=judge,
        budget_team_id=_identifier("verification.budget.team_id",
                                   raw_budget.get("team_id", "quality-verifier")),
        budget_feature=_identifier("verification.budget.feature",
                                   raw_budget.get("feature", "verification")),
        max_attempts=_positive_int("verification.max_attempts", raw.get("max_attempts", 3)),
        job_timeout_s=job_timeout, stream=stream, dead_letter_stream=dead_letter,
        consumer_group=str(raw.get("consumer_group", "verifiers")),
        maxlen=_positive_int("verification.maxlen", raw.get("maxlen", 10000)))

    # --- escalation
    raw = data.get("escalation") or {}
    raw_pre = raw.get("pre_call") or {}
    raw_post = raw.get("post_call") or {}
    priorities = tuple(raw_pre.get("priorities") or ())
    for p in priorities:
        _one_of("escalation.pre_call.priorities[]", p, tuple(x.value for x in Priority))
    checks = tuple(raw_post.get("checks") or ())
    for check in checks:
        _one_of("escalation.post_call.checks[]", check, POST_CALL_CHECKS)
    max_escalations = int(raw.get("max_escalations", 1))
    if not 0 <= max_escalations <= 2:
        raise QualityConfigError("escalation.max_escalations must be 0, 1 or 2")
    escalation = EscalationConfig(
        enabled=bool(raw.get("enabled", True)), pre_call_priorities=priorities,
        pre_call_confidence_below=_fraction("escalation.pre_call.confidence_below",
                                            raw_pre.get("confidence_below", 0.6)),
        post_call_checks=checks, max_escalations=max_escalations,
        refusal_patterns=tuple(str(p).lower() for p in raw.get("refusal_patterns") or ()))

    # --- privacy
    raw = data.get("privacy") or {}
    privacy = PrivacyConfig(
        store_prompts=bool(raw.get("store_prompts", True)),
        max_prompt_chars=_positive_int("privacy.max_prompt_chars",
                                       raw.get("max_prompt_chars", 2000)),
        prompt_preview_chars=_non_negative_int("privacy.prompt_preview_chars",
                                               raw.get("prompt_preview_chars", 120)))

    return QualityConfig(verify_enabled=verify_enabled, sampling=sampling,
                         verification=verification, escalation=escalation, privacy=privacy)
