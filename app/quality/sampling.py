"""Which successful responses get verified asynchronously.

Deterministic: the decision is a pure function of the request id, so the same request always
gets the same answer (reproducible in tests, stable across retries and replays), yet across
many requests the sampled fraction converges to the configured rate.
"""
import hashlib
from dataclasses import dataclass

from app.quality.config import SamplingConfig


def hash_fraction(key: str) -> float:
    """Map a string to a uniform-looking number in [0, 1) with SHA-256 (first 64 bits)."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


@dataclass(frozen=True)
class SampleDecision:
    sampled: bool
    rate: float                 # the rate that applied (0 when ineligible)
    reason: str
    fraction: float | None = None

    def to_dict(self) -> dict:
        d = {"sampled": self.sampled, "rate": self.rate, "reason": self.reason}
        if self.fraction is not None:
            d["fraction"] = round(self.fraction, 6)
        return d


def decide_sampling(cfg: SamplingConfig, *, request_id: str, source: str, final_tier: int,
                    confidence: float | None, escalated: bool = False,
                    verify_enabled: bool = True) -> SampleDecision:
    if not verify_enabled or not cfg.enabled:
        return SampleDecision(False, 0.0, "sampling disabled")
    if escalated:
        # The cascade already replaced the cheap answer: there is nothing cheap left to judge
        return SampleDecision(False, 0.0, "already escalated synchronously")
    if source in cfg.skip_sources:
        return SampleDecision(False, 0.0, f"routing source '{source}' is not sampled")
    if cfg.skip_top_tier and final_tier >= 3:
        return SampleDecision(False, 0.0, "already on the top tier")

    low_confidence = confidence is not None and confidence < cfg.low_confidence_threshold
    rate = cfg.low_confidence_rate if low_confidence else cfg.base_rate
    fraction = hash_fraction(request_id)
    sampled = fraction < rate
    label = "low-confidence" if low_confidence else "base"
    return SampleDecision(sampled, rate,
                          f"{label} rate {rate:.0%}: {'sampled' if sampled else 'not sampled'}",
                          fraction)
