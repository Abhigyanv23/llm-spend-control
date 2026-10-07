from decimal import Decimal

import pytest

from app.money import (
    NANOS_PER_USD,
    format_usd,
    nanos_to_usd,
    quantize_usd,
    to_decimal,
    usd_str,
    usd_to_nanos,
)


def test_float_drift_is_real_and_decimal_avoids_it():
    assert 0.1 + 0.2 != 0.3                             # 0.30000000000000004
    total = 0.0
    for _ in range(10):                                  # a running total, like a counter
        total += 0.1
    assert total != 1.0                                  # 0.9999999999999999
    # (Python 3.12+ sum() uses compensated summation and hides this; += does not)
    assert sum([Decimal("0.1")] * 10) == Decimal("1.0")


def test_to_decimal_goes_through_str_for_floats():
    assert to_decimal(0.1) == Decimal("0.1")
    assert Decimal(0.1) != Decimal("0.1")                # the trap to_decimal avoids


@pytest.mark.parametrize("usd, nanos", [
    (Decimal("1"), NANOS_PER_USD),
    (Decimal("0.00000390"), 3_900),
    (Decimal("0.00000001"), 10),                         # smallest NUMERIC(14,8) step
    (Decimal("123456.78901234"), 123_456_789_012_340),
    ("0", 0),
])
def test_usd_to_nanos(usd, nanos):
    assert usd_to_nanos(usd) == nanos


def test_nanos_round_half_up():
    assert usd_to_nanos(Decimal("0.0000000005")) == 1
    assert usd_to_nanos(Decimal("0.0000000004")) == 0


@pytest.mark.parametrize("usd", ["0.00000390", "5.00000000", "0.12345678", "999999.99999999"])
def test_round_trip_is_exact_at_8_decimal_places(usd):
    assert nanos_to_usd(usd_to_nanos(Decimal(usd))) == Decimal(usd)


def test_many_small_costs_add_up_exactly_in_nanos():
    per_request = Decimal("0.00000390")
    total_nanos = sum(usd_to_nanos(per_request) for _ in range(1_000_000))
    assert nanos_to_usd(total_nanos) == Decimal("3.90000000")


def test_quantize_and_format():
    assert quantize_usd(Decimal("0.000000015")) == Decimal("0.00000002")
    assert format_usd(Decimal("5.00000000")) == "$5.00"
    assert format_usd(Decimal("12.5")) == "$12.50"
    assert format_usd(Decimal("0.00040070")) == "$0.0004007"


def test_usd_str_never_uses_scientific_notation():
    # Regression: str(Decimal) gives "0E-8" / "3.9E-7" for values below 1e-6
    assert str(Decimal("0.00000000")) == "0E-8"
    assert usd_str(Decimal(0)) == "0.00000000"
    assert usd_str(Decimal("0.00000039")) == "0.00000039"
    assert usd_str(Decimal("12.5")) == "12.50000000"
