"""Budget time windows. Always UTC calendar day / UTC calendar month.

Why UTC: servers, containers and developers live in different time zones and some zones
have daylight-saving jumps (23h or 25h days). UTC gives every instance the same
"today" and makes the reset time unambiguous.
"""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Keys live a little longer than their period so a request that reserved at 23:59:59
# can still settle against the SAME day key after midnight, and so yesterday's numbers
# stay inspectable for a while.
TTL_GRACE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Period:
    name: str               # "day" | "month"
    key: str                # "2026-09-30" | "2026-09"
    start: datetime         # inclusive
    resets_at: datetime     # exclusive end = start of next period

    def ttl_seconds(self, now: datetime) -> int:
        return int((self.resets_at - now).total_seconds()) + TTL_GRACE_SECONDS


def as_utc(moment: datetime) -> datetime:
    """Treat naive datetimes as UTC; convert aware ones to UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def day_period(now: datetime) -> Period:
    now = as_utc(now)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return Period("day", start.strftime("%Y-%m-%d"), start, start + timedelta(days=1))


def month_period(now: datetime) -> Period:
    now = as_utc(now)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Day 28 + 4 days always lands in the next month; then snap back to its first day
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return Period("month", start.strftime("%Y-%m"), start, next_month)


def current_periods(now: datetime) -> list[Period]:
    return [day_period(now), month_period(now)]
