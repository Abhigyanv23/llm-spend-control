from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from app.budgets.periods import TTL_GRACE_SECONDS, current_periods, day_period, month_period
from app.providers.mock_adapter import MockAdapter
from app.registry import ModelRegistry
from app.schemas import ChatRequest, Message
from app.tokens import MESSAGE_OVERHEAD_TOKENS, estimate_input_tokens

registry = ModelRegistry("config/models.yaml")


def make_request(text: str = "Hello gateway", max_tokens: int = 512) -> ChatRequest:
    return ChatRequest(team_id="t", feature="f", max_tokens=max_tokens,
                       messages=[Message(role="user", content=text)])


def test_registry_prices_are_exact_decimals():
    spec = registry.get("gpt-4o-mini")
    assert spec.input_cost_per_mtok == Decimal("0.15")
    assert isinstance(spec.output_cost_per_mtok, Decimal)


def test_cost_is_exact():
    spec = registry.get("gpt-4o-mini")      # $0.15 in / $0.60 out per MTok (illustrative)
    # (1000 * 0.15 + 500 * 0.60) / 1e6 = 0.00045
    assert spec.cost(1000, 500) == Decimal("0.00045000")


def test_worst_case_prices_all_of_max_tokens_at_output_rate():
    spec = registry.get("mock-echo")        # $0.10 in / $0.40 out per MTok
    assert spec.worst_case_cost(7, 1000) == Decimal("0.00040070")


def test_input_estimate_includes_per_message_overhead():
    assert estimate_input_tokens(make_request("Hello gateway").messages) == 3 + MESSAGE_OVERHEAD_TOKENS
    two = [Message(role="system", content="x" * 40), Message(role="user", content="y" * 8)]
    assert estimate_input_tokens(two) == 10 + 2 + 2 * MESSAGE_OVERHEAD_TOKENS


async def test_worst_case_estimate_is_an_upper_bound_for_mock_provider():
    spec = registry.get("mock-echo")
    for text, max_tokens in [("Hello gateway", 512), ("a" * 5000, 16), ("hi", 1)]:
        request = make_request(text, max_tokens)
        result = await MockAdapter().complete(request, spec.name)
        estimate = spec.worst_case_cost(estimate_input_tokens(request.messages), max_tokens)
        assert spec.cost(result.input_tokens, result.output_tokens) <= estimate


def test_periods_are_utc_calendar_windows():
    now = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
    day, month = current_periods(now)
    assert (day.key, day.resets_at) == ("2026-09-30", datetime(2026, 10, 1, tzinfo=UTC))
    assert (month.key, month.resets_at) == ("2026-09", datetime(2026, 10, 1, tzinfo=UTC))
    assert day.ttl_seconds(now) == 1 + TTL_GRACE_SECONDS


def test_month_rollover_edge_cases():
    assert month_period(datetime(2026, 12, 15, tzinfo=UTC)).resets_at == datetime(2027, 1, 1, tzinfo=UTC)
    assert month_period(datetime(2028, 2, 29, tzinfo=UTC)).resets_at == datetime(2028, 3, 1, tzinfo=UTC)
    assert month_period(datetime(2026, 1, 31, tzinfo=UTC)).resets_at == datetime(2026, 2, 1, tzinfo=UTC)


def test_non_utc_input_is_converted_to_utc():
    ist = timezone(timedelta(hours=5, minutes=30))
    # 02:00 on Oct 1 in India is still Sept 30 in UTC
    assert day_period(datetime(2026, 10, 1, 2, 0, tzinfo=ist)).key == "2026-09-30"
