from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """Canonical request: the ONLY format callers ever send."""
    messages: list[Message] = Field(min_length=1)
    team_id: str = Field(min_length=1)
    feature: str = Field(min_length=1)
    priority: Priority = Priority.normal
    model: str | None = None          # optional preference; router decides later
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(BaseModel):
    """Canonical response: identical shape no matter which provider answered."""
    request_id: str
    model: str
    provider: str
    output: str
    usage: Usage
    cost_usd: float
    latency_ms: float
    metadata: dict = Field(default_factory=dict)