"""A small in-process TTL cache for analytics responses.

Why cache: dashboard pages fire several aggregate queries, often repeatedly (every viewer,
every refresh). The numbers move slowly, so serving a result up to `ttl` seconds old is fine.
Why in-process: zero infrastructure and no serialisation. The trade-off: each API process
has its own cache (two processes may show slightly different numbers for a few seconds), and
there is no explicit invalidation: data written now appears after at most `ttl` seconds.
With several API instances, a shared Redis cache would make results consistent.
"""
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any


class TTLCache:
    def __init__(self, ttl_s: float = 30.0, max_entries: int = 256):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._items: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> tuple[bool, Any]:
        item = self._items.get(key)
        if item is None or item[0] < time.monotonic():
            self._items.pop(key, None)
            self.misses += 1
            return False, None
        self._items.move_to_end(key)                # LRU: recently used stays
        self.hits += 1
        return True, item[1]

    def set(self, key: tuple, value: Any) -> None:
        if self.ttl_s <= 0:
            return
        self._items[key] = (time.monotonic() + self.ttl_s, value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)         # evict least recently used

    async def get_or_compute(self, key: tuple,
                             compute: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
        """Returns (value, was_cached)."""
        found, value = self.get(key)
        if found:
            return value, True
        value = await compute()
        self.set(key, value)
        return value, False

    def clear(self) -> None:
        self._items.clear()
