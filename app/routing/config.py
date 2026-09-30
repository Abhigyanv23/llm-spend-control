"""Routing configuration: candidate models per tier, per-feature rules, classifier keywords.

Loaded once at startup and validated against the model registry. A bad config stops the
server with a clear message instead of failing on live traffic (fail fast).
"""
from dataclasses import asdict, dataclass

import yaml

from app.errors import UnknownModelError
from app.registry import ModelRegistry
from app.schemas import Priority

TIERS = (1, 2, 3)


class RoutingConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FeatureRule:
    min_tier: int = 1
    max_tier: int = 3
    pin_model: str | None = None
    requires: tuple[str, ...] = ()
    budget_downgrade: bool | None = None     # None = use the global default
    reason: str | None = None


DEFAULT_RULE = FeatureRule()


@dataclass(frozen=True)
class ClassifierConfig:
    keywords: dict[int, tuple[str, ...]]     # tier -> instruction keywords
    risk_keywords: tuple[str, ...] = ()
    structured_output_keywords: tuple[str, ...] = ()
    long_context_tokens: int = 6000


@dataclass(frozen=True)
class RoutingConfig:
    profile: str
    tiers: dict[int, tuple[str, ...]]        # tier -> candidate model names, in preference order
    baseline_model: str
    budget_downgrade: bool
    priority_min_tier: dict[str, int]
    features: dict[str, FeatureRule]
    classifier: ClassifierConfig
    tier_descriptions: dict[int, str]

    def rule_for(self, feature: str) -> FeatureRule:
        return self.features.get(feature, DEFAULT_RULE)

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "tiers": {str(t): {"description": self.tier_descriptions.get(t, ""),
                               "models": list(models)} for t, models in self.tiers.items()},
            "baseline_model": self.baseline_model,
            "budget_downgrade": self.budget_downgrade,
            "priority_min_tier": dict(self.priority_min_tier),
            "features": {name: {**asdict(rule), "requires": list(rule.requires)}
                         for name, rule in self.features.items()},
        }


def _tier_value(mapping: dict, tier: int):
    # YAML keys `1:` parse as ints; accept "1" strings too
    return mapping.get(tier, mapping.get(str(tier)))


def _require_model(registry: ModelRegistry, name: str, where: str) -> None:
    try:
        registry.get(name)
    except UnknownModelError:
        raise RoutingConfigError(f"{where}: model '{name}' is not in the model registry") from None


def load_routing_config(path: str, profile: str, registry: ModelRegistry) -> RoutingConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    profiles = data.get("profiles") or {}
    if profile not in profiles:
        raise RoutingConfigError(
            f"Routing profile '{profile}' not found in {path} (available: {sorted(profiles)})")
    raw_tiers = profiles[profile].get("tiers") or {}

    tiers: dict[int, tuple[str, ...]] = {}
    for t in TIERS:
        models = tuple(_tier_value(raw_tiers, t) or ())
        if not models:
            raise RoutingConfigError(f"Profile '{profile}' has no models for tier {t}")
        for name in models:
            _require_model(registry, name, f"profile '{profile}' tier {t}")
        tiers[t] = models

    baseline = profiles[profile].get("baseline_model") or tiers[3][0]
    _require_model(registry, baseline, f"profile '{profile}' baseline_model")

    features: dict[str, FeatureRule] = {}
    for name, raw in (data.get("features") or {}).items():
        raw = raw or {}
        rule = FeatureRule(min_tier=int(raw.get("min_tier", 1)),
                           max_tier=int(raw.get("max_tier", 3)),
                           pin_model=raw.get("pin_model"),
                           requires=tuple(raw.get("requires") or ()),
                           budget_downgrade=raw.get("budget_downgrade"),
                           reason=raw.get("reason"))
        if not 1 <= rule.min_tier <= rule.max_tier <= 3:
            raise RoutingConfigError(
                f"Feature '{name}': need 1 <= min_tier <= max_tier <= 3 "
                f"(got {rule.min_tier}..{rule.max_tier})")
        if rule.pin_model:
            _require_model(registry, rule.pin_model, f"feature '{name}' pin_model")
        features[name] = rule

    valid_priorities = {p.value for p in Priority}
    priority_min_tier: dict[str, int] = {}
    for key, value in (data.get("priority_min_tier") or {}).items():
        if key not in valid_priorities or int(value) not in TIERS:
            raise RoutingConfigError(f"priority_min_tier: invalid entry {key}: {value}")
        priority_min_tier[key] = int(value)

    raw_clf = data.get("classifier") or {}
    raw_kw = raw_clf.get("keywords") or {}
    classifier = ClassifierConfig(
        keywords={t: tuple(str(k).lower() for k in (_tier_value(raw_kw, t) or ()))
                  for t in TIERS},
        risk_keywords=tuple(str(k).lower() for k in raw_clf.get("risk_keywords") or ()),
        structured_output_keywords=tuple(
            str(k).lower() for k in raw_clf.get("structured_output_keywords") or ()),
        long_context_tokens=int(raw_clf.get("long_context_tokens", 6000)),
    )

    raw_desc = data.get("tier_descriptions") or {}
    return RoutingConfig(
        profile=profile, tiers=tiers, baseline_model=baseline,
        budget_downgrade=bool(data.get("budget_downgrade", True)),
        priority_min_tier=priority_min_tier, features=features, classifier=classifier,
        tier_descriptions={t: str(_tier_value(raw_desc, t) or "") for t in TIERS},
    )