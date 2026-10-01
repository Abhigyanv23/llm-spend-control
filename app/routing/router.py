"""Complexity-based model router.

Decision order (first match wins; later steps only narrow the choice):
  1. explicit  the caller named a model -> honoured as-is
  2. pinned    a feature rule pins a model -> used as-is
  3. routed    the classifier picks a tier, clamped to the feature's [min_tier, max_tier] and
               the optional priority floor; the first candidate in that tier that is available,
               fits the context window and has the required capabilities is chosen
Cheaper allowed tiers become `fallbacks`: the gateway downgrades to them instead of blocking
when a budget limit is hit.
"""
from dataclasses import dataclass

from app.errors import ContextTooLongError, NoRouteError
from app.registry import ModelRegistry, ModelSpec
from app.routing.classifier import Classification, RuleBasedClassifier
from app.routing.config import FeatureRule, RoutingConfig
from app.schemas import ChatRequest
from app.tokens import estimate_input_tokens


def available_providers(settings) -> set[str]:
    """Providers the gateway can actually call. Keyless providers are always available."""
    providers = {"mock", "ollama"}
    if settings.openai_api_key:
        providers.add("openai")
    if settings.anthropic_api_key:
        providers.add("anthropic")
    return providers


@dataclass(frozen=True)
class RouteDecision:
    model: str
    tier: int
    source: str                         # explicit | pinned | routed | default
    min_tier: int
    max_tier: int
    fallbacks: tuple[str, ...] = ()     # cheaper models allowed for budget downgrade, in order
    reasons: tuple[str, ...] = ()
    classification: Classification | None = None

    def metadata(self) -> dict:
        meta = {"model": self.model, "tier": self.tier, "source": self.source,
                "min_tier": self.min_tier, "max_tier": self.max_tier,
                "fallbacks": list(self.fallbacks), "reasons": list(self.reasons)}
        if self.classification is not None:
            meta["classifier"] = self.classification.to_dict()
        return meta


class Router:
    def __init__(self, registry: ModelRegistry, config: RoutingConfig, available: set[str],
                 classifier: RuleBasedClassifier | None = None):
        self.registry = registry
        self.config = config
        self.available_providers = set(available)
        self.classifier = classifier or RuleBasedClassifier(config.classifier)

    @property
    def baseline(self) -> ModelSpec:
        """The counterfactual 'send everything to the strongest model' choice."""
        return self.registry.get(self.config.baseline_model)

    def route(self, request: ChatRequest) -> RouteDecision:
        # 1. Explicit model: the caller knows best (UnknownModelError -> 400 as before)
        if request.model:
            spec = self.registry.get(request.model)
            return RouteDecision(model=spec.name, tier=spec.tier, source="explicit",
                                 min_tier=spec.tier, max_tier=spec.tier,
                                 reasons=("model requested explicitly by the caller",))

        # 2. Pinned feature
        rule = self.config.rule_for(request.feature)
        if rule.pin_model:
            spec = self.registry.get(rule.pin_model)
            return RouteDecision(
                model=spec.name, tier=spec.tier, source="pinned",
                min_tier=spec.tier, max_tier=spec.tier,
                reasons=(rule.reason or f"feature '{request.feature}' is pinned to {spec.name}",))

        # 3. Classifier tier within the allowed bounds
        classification = self.classifier.classify(request)
        reasons: list[str] = []
        min_tier, max_tier = rule.min_tier, rule.max_tier
        if rule.min_tier > 1 or rule.max_tier < 3:
            reasons.append(rule.reason or f"feature '{request.feature}' allows tiers "
                                          f"{rule.min_tier}-{rule.max_tier}")
        floor = self.config.priority_min_tier.get(request.priority.value, 1)
        if floor > min_tier:
            min_tier = floor
            max_tier = max(max_tier, floor)     # an explicit priority floor beats a feature cap
            reasons.append(f"priority '{request.priority.value}' requires tier >= {floor}")

        tier = min(max(classification.tier, min_tier), max_tier)
        if tier != classification.tier:
            reasons.append(f"classifier chose tier {classification.tier}; clamped to {tier}")

        # 4. First usable candidate; escalate only if the tier has none, never above max_tier
        needed = estimate_input_tokens(request.messages) + request.max_tokens
        spec: ModelSpec | None = None
        for t in range(tier, max_tier + 1):
            spec = self._pick(t, needed, rule)
            if spec is not None:
                if t != tier:
                    reasons.append(f"no usable tier-{tier} model; escalated to tier {t}")
                    tier = t
                break
        if spec is None:
            raise self._no_route_error(range(tier, max_tier + 1), needed, rule)

        # 5. Cheaper fallbacks for budget downgrade (never below min_tier)
        downgrade = (self.config.budget_downgrade if rule.budget_downgrade is None
                     else rule.budget_downgrade)
        fallbacks: list[str] = []
        if downgrade:
            for t in range(tier - 1, min_tier - 1, -1):
                cheaper = self._pick(t, needed, rule)
                if cheaper and cheaper.name != spec.name and cheaper.name not in fallbacks:
                    fallbacks.append(cheaper.name)

        return RouteDecision(model=spec.name, tier=tier, source="routed", min_tier=min_tier,
                             max_tier=max_tier, fallbacks=tuple(fallbacks),
                             reasons=tuple(reasons), classification=classification)

    def downgrade_enabled(self, feature: str) -> bool:
        rule = self.config.rule_for(feature)
        return (self.config.budget_downgrade if rule.budget_downgrade is None
                else rule.budget_downgrade)

    def pick_from_tier(self, request: ChatRequest, start_tier: int,
                       max_tier: int) -> ModelSpec | None:
        """First usable model at start_tier or above (never above max_tier). The escalation
        mechanism: WHEN to escalate is quality policy (app/quality/escalation.py)."""
        rule = self.config.rule_for(request.feature)
        needed = estimate_input_tokens(request.messages) + request.max_tokens
        for t in range(max(start_tier, 1), min(max_tier, 3) + 1):
            spec = self._pick(t, needed, rule)
            if spec is not None:
                return spec
        return None

    def _usable(self, spec: ModelSpec, rule: FeatureRule) -> bool:
        return (spec.provider in self.available_providers
                and set(rule.requires) <= set(spec.supports))

    def _pick(self, tier: int, needed_tokens: int, rule: FeatureRule) -> ModelSpec | None:
        for name in self.config.tiers[tier]:
            spec = self.registry.get(name)
            if self._usable(spec, rule) and spec.max_context >= needed_tokens:
                return spec
        return None

    def _no_route_error(self, tiers: range, needed: int, rule: FeatureRule) -> Exception:
        usable = [self.registry.get(n) for t in tiers for n in self.config.tiers[t]]
        usable = [s for s in usable if self._usable(s, rule)]
        if usable and all(s.max_context < needed for s in usable):
            # The only problem is size: keep Phase 1's 400 context_too_long contract
            largest = max(usable, key=lambda s: s.max_context)
            return ContextTooLongError(largest.name, needed, largest.max_context)
        needs = f"~{needed} tokens of context"
        if rule.requires:
            needs += f" and {', '.join(rule.requires)} support"
        return NoRouteError(
            f"No available model in tier(s) {tiers.start}-{tiers.stop - 1} of routing profile "
            f"'{self.config.profile}' can serve this request (needs {needs}). "
            f"Check your API keys and config/routing.yaml.")