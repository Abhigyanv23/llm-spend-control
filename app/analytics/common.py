"""Shared helpers for the analytics layer: filters, UTC day buckets, statistics.

Analytics functions are pure query functions (no HTTP). They return Python types (Decimal,
date, datetime); the API layer turns them into JSON. That keeps the maths testable with exact
values and the HTTP concerns in one place.
"""
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func

from app.money import quantize_usd

ZERO = Decimal(0)
Z95 = 1.96


@dataclass(frozen=True)
class AnalyticsFilter:
    start: datetime                 # inclusive, UTC
    end: datetime                   # exclusive, UTC
    team_id: str | None = None
    feature: str | None = None

    def conditions(self, model) -> list:
        """WHERE clauses for any table with created_at / team_id / feature columns."""
        conds = [model.created_at >= self.start, model.created_at < self.end]
        if self.team_id is not None:
            conds.append(model.team_id == self.team_id)
        if self.feature is not None:
            conds.append(model.feature == self.feature)
        return conds

    def days(self) -> list[date]:
        """Every UTC calendar day touched by the window, for zero-filling time series."""
        first, last = self.start.date(), (self.end - timedelta(microseconds=1)).date()
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]


class WindowError(ValueError):
    pass


def make_filter(start: datetime | None, end: datetime | None, *, team_id: str | None = None,
                feature: str | None = None, default_days: int = 30, max_days: int = 366,
                now: datetime | None = None) -> AnalyticsFilter:
    """Defaults: the last `default_days` up to now. Naive datetimes are treated as UTC."""
    now = now or datetime.now(UTC)
    end = as_utc(end) if end else now
    start = as_utc(start) if start else end - timedelta(days=default_days)
    if start >= end:
        raise WindowError("'from' must be earlier than 'to'")
    if end - start > timedelta(days=max_days):
        raise WindowError(f"window is longer than {max_days} days")
    return AnalyticsFilter(start=start, end=end, team_id=team_id, feature=feature)


def as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def dialect_of(session) -> str:
    return session.bind.dialect.name


def day_bucket(column, dialect: str):
    """SQL expression for the UTC calendar day of a timestamp."""
    if dialect == "postgresql":
        # timestamptz -> wall-clock time in UTC -> date (independent of the server timezone)
        return func.date(func.timezone("UTC", column))
    return func.date(column)        # SQLite stores UTC timestamps as text: YYYY-MM-DD ...


def to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def money(value) -> Decimal:
    """SUM() results: None -> 0; SQLite floats -> exact 8-dp Decimal."""
    return quantize_usd(value or 0)


def ratio(numerator, denominator) -> float | None:
    return round(float(numerator) / float(denominator), 4) if denominator else None


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion.

    Better than the normal approximation p ± z·√(p(1−p)/n) for small n or p near 0/1:
    it never leaves [0, 1] and isn't zero-width when p is 0 or 1 (2/2 passes is not
    "certainly 100%")."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def percentile_cont(values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile, identical to Postgres percentile_cont(q)."""
    if not values:
        return None
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 2)
