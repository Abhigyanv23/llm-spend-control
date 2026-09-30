from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.money import usd_str

# team_id / feature become parts of Redis keys and HTTP headers, so keep them to a safe charset
ID_PATTERN = r"^[A-Za-z0-9._-]+$"
ID_MAX_LENGTH = 64


# Money in API responses: a Decimal serialised as a fixed-point JSON string ("0.00000390")
USD = Annotated[Decimal, PlainSerializer(usd_str, return_type=str, when_used="json")]


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"

    @property
    def can_override_budget(self) -> bool:
        return self in (Priority.high, Priority.critical)


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """Canonical request: the ONLY format callers ever send."""
    messages: list[Message] = Field(min_length=1)
    team_id: str = Field(min_length=1, max_length=ID_MAX_LENGTH, pattern=ID_PATTERN)
    feature: str = Field(min_length=1, max_length=ID_MAX_LENGTH, pattern=ID_PATTERN)
    priority: Priority = Priority.normal
    model: str | None = None          # optional preference; router decides later
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(BaseModel):
    """Canonical response: identical shape no matter which provider answered.
    Money is a Decimal and is serialised as a JSON string (e.g. "0.00000390")."""
    request_id: str
    model: str
    provider: str
    output: str
    usage: Usage
    cost_usd: USD
    latency_ms: float
    metadata: dict = Field(default_factory=dict)


# ---------------------------------------------------------------- budgets (Phase 2)

class BudgetScope(str, Enum):
    team = "team"
    feature = "feature"


Money = Field(default=None, ge=0, max_digits=14, decimal_places=8)


class BudgetPolicyIn(BaseModel):
    """Body of PUT /v1/budgets/{scope}/{scope_id}. A null limit means unlimited."""
    daily_limit_usd: Decimal | None = Money
    monthly_limit_usd: Decimal | None = Money
    enabled: bool = True


class BudgetPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scope: BudgetScope
    scope_id: str
    daily_limit_usd: USD | None
    monthly_limit_usd: USD | None
    enabled: bool
    updated_at: datetime | None = None


class PeriodStatus(BaseModel):
    period: Literal["day", "month"]
    period_key: str                     # "2026-09-30" or "2026-09"
    spent_usd: USD
    reserved_usd: USD                   # in-flight requests not yet settled
    limit_usd: USD | None               # None = unlimited
    percent_used: float | None          # (spent + reserved) / limit * 100
    remaining_usd: USD | None
    resets_at: datetime


class BudgetStatusOut(BaseModel):
    scope: BudgetScope
    scope_id: str
    policy: BudgetPolicyOut | None
    day: PeriodStatus
    month: PeriodStatus


# ---------------------------------------------------------------- usage (Phase 2)

class UsageRow(BaseModel):
    request_id: str
    created_at: datetime
    team_id: str
    feature: str
    priority: str
    model: str | None
    provider: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: USD
    cost_usd: USD
    latency_ms: float | None
    status: str
    error_code: str | None
    override_reason: str | None
    metadata: dict


class UsageTotals(BaseModel):
    count: int
    cost_usd: USD
    input_tokens: int
    output_tokens: int


class UsagePage(BaseModel):
    items: list[UsageRow]
    totals: UsageTotals                 # totals over ALL rows matching the filters, not just this page
    limit: int
    offset: int
    has_more: bool
