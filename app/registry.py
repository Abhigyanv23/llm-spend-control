from dataclasses import asdict, dataclass

import yaml

from app.errors import UnknownModelError


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: str
    tier: int                      # 1 = cheap/simple, 2 = mid, 3 = strongest
    input_cost_per_mtok: float
    output_cost_per_mtok: float
    avg_latency_ms: int
    max_context: int
    supports: tuple[str, ...] = ()

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        raw = (input_tokens * self.input_cost_per_mtok
               + output_tokens * self.output_cost_per_mtok) / 1_000_000
        return round(raw, 8)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["supports"] = list(self.supports)
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