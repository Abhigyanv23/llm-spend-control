"""Redis counters for real-time budget enforcement. Money is integer nano-dollars.

Key layout (one pair per scope + period):
    budget:team:search:day:2026-09-30:spent
    budget:team:search:day:2026-09-30:reserved
    budget:feature:summarize:month:2026-09:spent
    ...
"""
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis

from app.budgets.lua import RESERVE_LUA, SETTLE_LUA
from app.budgets.periods import Period

UNLIMITED = -1


@dataclass(frozen=True)
class Counter:
    """One (scope, scope_id, period) bucket in Redis."""
    scope: str
    scope_id: str
    period: Period

    @property
    def base_key(self) -> str:
        return f"budget:{self.scope}:{self.scope_id}:{self.period.name}:{self.period.key}"

    @property
    def spent_key(self) -> str:
        return f"{self.base_key}:spent"

    @property
    def reserved_key(self) -> str:
        return f"{self.base_key}:reserved"


@dataclass(frozen=True)
class CounterValues:
    spent: int       # nano-dollars
    reserved: int    # nano-dollars


def _keys(counters: list[Counter]) -> list[str]:
    keys: list[str] = []
    for c in counters:
        keys += [c.spent_key, c.reserved_key]
    return keys


class RedisBudgetStore:
    def __init__(self, client: Redis):
        self.client = client
        # register_script -> EVALSHA (script cached server-side), auto-falls back to EVAL
        self._reserve = client.register_script(RESERVE_LUA)
        self._settle = client.register_script(SETTLE_LUA)

    async def load_scripts(self) -> None:
        """Preload both scripts at startup: the first request doesn't pay for SCRIPT LOAD,
        and a Lua syntax error fails loudly at boot instead of on the first request."""
        for script in (self._reserve, self._settle):
            await self.client.script_load(script.script)

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def reserve(self, counters: list[Counter], limits: list[int | None], amount: int,
                      allow_over: bool, now: datetime) -> tuple[bool, list[CounterValues]]:
        """Atomically check every limit and, if all pass (or allow_over), hold `amount`
        on every counter. Returns (allowed, values seen before the reservation)."""
        args: list[int | str] = [amount, "1" if allow_over else "0"]
        for c, limit in zip(counters, limits, strict=True):
            args += [c.period.ttl_seconds(now), UNLIMITED if limit is None else limit]
        raw = await self._reserve(keys=_keys(counters), args=args)
        values = [CounterValues(int(raw[1 + 2 * i]), int(raw[2 + 2 * i]))
                  for i in range(len(counters))]
        return bool(int(raw[0])), values

    async def settle(self, counters: list[Counter], reserved: int, actual: int,
                     now: datetime) -> None:
        args: list[int] = [reserved, actual] + [c.period.ttl_seconds(now) for c in counters]
        await self._settle(keys=_keys(counters), args=args)

    async def read(self, counters: list[Counter]) -> list[CounterValues]:
        raw = await self.client.mget(_keys(counters))
        return [CounterValues(int(raw[2 * i] or 0), int(raw[2 * i + 1] or 0))
                for i in range(len(counters))]

    async def claim_once(self, key: str, ttl_seconds: int) -> bool:
        """SET NX: True only for the first caller. Used to deduplicate alerts."""
        return bool(await self.client.set(key, "1", nx=True, ex=ttl_seconds))

    async def rebuild(self, periods: list[Period], spent: dict[Counter, int],
                      now: datetime) -> int:
        """Replace ALL counters of the given periods with `spent` (reserved -> 0).
        Runs as one MULTI/EXEC transaction so readers never see a half-rebuilt state."""
        stale: set[str] = set()
        for p in periods:
            async for key in self.client.scan_iter(match=f"budget:*:{p.name}:{p.key}:*",
                                                   count=500):
                stale.add(key)
        async with self.client.pipeline(transaction=True) as pipe:
            if stale:
                pipe.delete(*stale)
            for counter, nanos in spent.items():
                pipe.set(counter.spent_key, nanos, ex=counter.period.ttl_seconds(now))
            await pipe.execute()
        return len(spent)
