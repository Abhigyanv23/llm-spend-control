from dataclasses import asdict, dataclass
from decimal import Decimal

import yaml

from app.errors import UnknownModelError
from app.money import TOKENS_PER_MTOK, quantize_usd, to_decimal


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: str
    tier: int                      # 1 = cheap/simple, 2 = mid, 3 = strongest
    input_cost_per_mtok: Decimal
    output_cost_per_mtok: Decimal
    avg_latency_ms: int
    max_context: int
    supports: tuple[str, ...] = ()

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Exact cost in USD, rounded to 8 decimal places (the NUMERIC(14, 8) scale)."""
        raw = (input_tokens * self.input_cost_per_mtok
               + output_tokens * self.output_cost_per_mtok) / TOKENS_PER_MTOK
        return quantize_usd(raw)

    def worst_case_cost(self, estimated_input_tokens: int, max_tokens: int) -> Decimal:
        """Upper bound used for budget reservation: assume the model uses ALL of max_tokens."""
        return self.cost(estimated_input_tokens, max_tokens)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["supports"] = list(self.supports)
        d["input_cost_per_mtok"] = str(self.input_cost_per_mtok)
        d["output_cost_per_mtok"] = str(self.output_cost_per_mtok)
        return d


class ModelRegistry:
    """Single source of truth for model facts. Loaded from YAML (DB later)."""

    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self._models: dict[str, ModelSpec] = {}
        for entry in data["models"]:
            entry = dict(entry)
            entry["supports"] = tuple(entry.get("supports") or [])
            # YAML parses 0.15 as a float; convert via str() so prices are exact Decimals
            entry["input_cost_per_mtok"] = to_decimal(entry["input_cost_per_mtok"])
            entry["output_cost_per_mtok"] = to_decimal(entry["output_cost_per_mtok"])
            spec = ModelSpec(**entry)
            self._models[spec.name] = spec

        self.default_model: str = data["defaults"]["model"]
        if self.default_model not in self._models:
            raise ValueError(f"Default model '{self.default_model}' not defined in registry")

    def get(self, name: str) -> ModelSpec:
        try:
            return self._models[name]
        except KeyError:
            raise UnknownModelError(name)

    def all(self) -> list[ModelSpec]:
        return list(self._models.values())

    def by_tier(self, tier: int) -> list[ModelSpec]:
        return [m for m in self._models.values() if m.tier == tier]
