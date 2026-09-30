"""Money helpers. From Phase 2 on the rule is:

    Python   -> decimal.Decimal
    Postgres -> NUMERIC(14, 8)
    Redis    -> integer nano-dollars (1 USD = 1_000_000_000)

Never float. Floats are binary fractions: 0.1 + 0.2 != 0.3, and the error compounds
when you add up millions of tiny per-request costs.
"""
from decimal import ROUND_HALF_UP, Decimal

NANOS_PER_USD = 1_000_000_000
USD_QUANTUM = Decimal("0.00000001")      # 8 decimal places, matches NUMERIC(14, 8)
TOKENS_PER_MTOK = Decimal(1_000_000)


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Convert safely. Floats go through str() so 0.1 becomes Decimal('0.1'),
    not Decimal('0.1000000000000000055511151231257827...')."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def quantize_usd(value: Decimal | int | float | str) -> Decimal:
    return to_decimal(value).quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)


def usd_to_nanos(usd: Decimal | int | float | str) -> int:
    return int((to_decimal(usd) * NANOS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def nanos_to_usd(nanos: int) -> Decimal:
    return quantize_usd(Decimal(nanos) / NANOS_PER_USD)


def usd_str(value: Decimal | int | float | str) -> str:
    """Fixed-point string for JSON/APIs. str(Decimal) switches to scientific notation
    below 1e-6 ("0E-8", "3.9E-7"), which clients would misparse; this never does."""
    return format(quantize_usd(value), "f")


def format_usd(value: Decimal | int | float | str) -> str:
    """Human-readable dollars for error messages: $5.00, $0.0004, $12.5 -> $12.50."""
    text = format(to_decimal(value).normalize(), "f")
    whole, _, frac = text.partition(".")
    return f"${whole}.{frac.ljust(2, '0')}"
