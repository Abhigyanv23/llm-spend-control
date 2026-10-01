"""The verification job: everything the worker needs, captured at response time.

The job is self-contained on purpose: the worker never reads request_logs (that row is written
by a background task and may not exist yet), and the payload is versioned so old jobs still
parse after a deploy.
"""
import json
from dataclasses import asdict, dataclass, field

JOB_VERSION = 1


@dataclass(frozen=True)
class VerificationJob:
    request_id: str
    created_at: str                     # ISO 8601 UTC, when the original request started
    team_id: str
    feature: str
    priority: str
    messages: list[dict]                # [{"role": ..., "content": ...}]
    output: str                         # the cheap answer being checked
    model: str                          # the model that produced it
    tier: int
    routing_source: str
    classifier_confidence: float | None = None
    classifier_features: dict = field(default_factory=dict)
    classifier_reasons: list[str] = field(default_factory=list)
    sample_rate: float | None = None
    version: int = JOB_VERSION

    def to_fields(self) -> dict[str, str]:
        """Redis stream entries are flat str -> str maps: one JSON field holds the payload,
        plus a couple of plain fields that are handy when inspecting with redis-cli."""
        return {"payload": json.dumps(asdict(self), ensure_ascii=False),
                "request_id": self.request_id, "v": str(self.version)}

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "VerificationJob":
        """Raises ValueError/KeyError/TypeError on a malformed entry (a poison message)."""
        data = json.loads(fields["payload"])
        if not isinstance(data, dict):
            raise ValueError("payload is not a JSON object")
        if int(data.get("version", 1)) > JOB_VERSION:
            raise ValueError(f"unsupported job version {data.get('version')}")
        job = cls(**data)
        if not job.messages or not isinstance(job.messages, list):
            raise ValueError("job has no messages")
        return job

    def prompt_text(self) -> str:
        """System prompts + the latest user message: the task, as the classifier saw it."""
        system = [m.get("content", "") for m in self.messages if m.get("role") == "system"]
        last_user = next((m.get("content", "") for m in reversed(self.messages)
                          if m.get("role") == "user"), "")
        return "\n".join([*system, last_user])
