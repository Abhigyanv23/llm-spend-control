"""The Lua scripts: atomic check-and-reserve, settle, release, rebuild."""
import asyncio
from datetime import UTC, datetime

from app.budgets.periods import current_periods
from app.budgets.store import Counter

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
DAY, MONTH = current_periods(NOW)
TEAM_DAY = Counter("team", "search", DAY)
FEATURE_MONTH = Counter("feature", "summarize", MONTH)


async def values(redis_client, counter: Counter) -> tuple[int, int]:
    spent, reserved = await redis_client.mget(counter.spent_key, counter.reserved_key)
    return int(spent or 0), int(reserved or 0)


async def test_reserve_within_limit_holds_amount(store, redis_client):
    allowed, seen = await store.reserve([TEAM_DAY], [1_000], 300, allow_over=False, now=NOW)
    assert allowed is True
    assert (seen[0].spent, seen[0].reserved) == (0, 0)          # values BEFORE the hold
    assert await values(redis_client, TEAM_DAY) == (0, 300)
    assert TEAM_DAY.reserved_key == "budget:team:search:day:2026-09-30:reserved"
    assert 0 < await redis_client.ttl(TEAM_DAY.reserved_key) <= DAY.ttl_seconds(NOW)


async def test_reserve_that_would_reach_limit_is_rejected_and_holds_nothing(store, redis_client):
    await store.reserve([TEAM_DAY], [1_000], 700, allow_over=False, now=NOW)
    allowed, seen = await store.reserve([TEAM_DAY], [1_000], 300, allow_over=False, now=NOW)
    assert allowed is False                                      # 700 + 300 >= 1000
    assert seen[0].reserved == 700
    assert await values(redis_client, TEAM_DAY) == (0, 700)      # unchanged


async def test_one_exceeded_counter_blocks_all_counters(store, redis_client):
    allowed, _ = await store.reserve([TEAM_DAY, FEATURE_MONTH], [None, 100], 500,
                                     allow_over=False, now=NOW)
    assert allowed is False
    assert await values(redis_client, TEAM_DAY) == (0, 0)        # all-or-nothing


async def test_unlimited_counters_are_still_tracked(store, redis_client):
    allowed, _ = await store.reserve([TEAM_DAY], [None], 10**12, allow_over=False, now=NOW)
    assert allowed is True
    assert await values(redis_client, TEAM_DAY) == (0, 10**12)


async def test_allow_over_reserves_despite_limit(store, redis_client):
    allowed, _ = await store.reserve([TEAM_DAY], [100], 500, allow_over=True, now=NOW)
    assert allowed is True
    assert await values(redis_client, TEAM_DAY) == (0, 500)


async def test_settle_swaps_hold_for_actual_cost(store, redis_client):
    await store.reserve([TEAM_DAY, FEATURE_MONTH], [None, None], 500, False, NOW)
    await store.settle([TEAM_DAY, FEATURE_MONTH], reserved=500, actual=42, now=NOW)
    assert await values(redis_client, TEAM_DAY) == (42, 0)
    assert await values(redis_client, FEATURE_MONTH) == (42, 0)
    assert await redis_client.ttl(TEAM_DAY.spent_key) > 0


async def test_release_adds_no_spend(store, redis_client):
    await store.reserve([TEAM_DAY], [None], 500, False, NOW)
    await store.settle([TEAM_DAY], reserved=500, actual=0, now=NOW)
    assert await values(redis_client, TEAM_DAY) == (0, 0)


async def test_settle_never_drives_reserved_negative(store, redis_client):
    await store.settle([TEAM_DAY], reserved=500, actual=10, now=NOW)   # no hold exists
    assert await values(redis_client, TEAM_DAY) == (10, 0)


async def test_large_amounts_stay_integers(store, redis_client):
    big = 10**15 + 7                                   # $1,000,000.000000007 in nanos
    await store.reserve([TEAM_DAY], [None], big, False, NOW)
    await store.settle([TEAM_DAY], reserved=big, actual=big, now=NOW)
    assert await values(redis_client, TEAM_DAY) == (big, 0)


async def test_concurrent_reservations_never_overshoot_the_limit(store):
    """50 requests race for a budget with room for exactly 10. The Lua script makes
    check-and-reserve one atomic step, so exactly 10 win."""
    amount = 100
    limit = 10 * amount + 1                            # room for 10 (blocking is at >= limit)
    results = await asyncio.gather(*[
        store.reserve([TEAM_DAY], [limit], amount, allow_over=False, now=NOW)
        for _ in range(50)])
    assert sum(allowed for allowed, _ in results) == 10


async def test_naive_get_then_set_overshoots(redis_client):
    """The bug the Lua script prevents: read, (network gap), write."""
    key, amount, limit = "naive:reserved", 100, 1_001

    async def naive_reserve() -> bool:
        current = int(await redis_client.get(key) or 0)
        await asyncio.sleep(0)                         # another request runs here
        if current + amount >= limit:
            return False
        await redis_client.set(key, current + amount)  # lost update: overwrites others
        return True

    admitted = sum(await asyncio.gather(*[naive_reserve() for _ in range(50)]))
    assert admitted > 10                               # every request saw "0 reserved"


async def test_rebuild_replaces_counters_and_clears_holds(store, redis_client):
    await store.reserve([TEAM_DAY], [None], 999, False, NOW)            # stuck hold
    stale = Counter("team", "ghost", DAY)
    await redis_client.set(stale.spent_key, 123)                        # not in Postgres
    await store.rebuild([DAY, MONTH], {TEAM_DAY: 5_000}, NOW)
    assert await values(redis_client, TEAM_DAY) == (5_000, 0)
    assert await redis_client.get(stale.spent_key) is None


async def test_claim_once_deduplicates(store):
    assert await store.claim_once("alert:x", 60) is True
    assert await store.claim_once("alert:x", 60) is False
