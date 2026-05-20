"""Generic 60s-TTL cache for IsoKron read paths.

Mirrors the TS-side ``CachingKoraRoleCharterReader`` pattern in
``packages/sb1-substrate-shapes/src/kora-role-charter.ts`` — per-key
in-memory TTL with eviction-on-read and an injectable clock function
for tests.

The cache is generic over the cached value type so the Role Charter
reader, policy registry reader, and capability matrix reader all share
one implementation. Capability matrix doesn't strictly need a cache
(it's a static Python const), but wiring it through the same code path
keeps the system_prompt_block assembler's surface uniform.

Async-safety: a single ``asyncio.Lock`` per (cache, key) is overkill
when multiple coroutines may race a cache miss — in that case both
issue queries, but the second write loses to the first by timestamp
order. Acceptable for the read-mostly substrate config path. If
reads become hot we revisit; flagged in ``[kora.isokron.todo]`` below.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Generic, Optional, TypeVar

T = TypeVar("T")

ClockFn = Callable[[], float]
"""Monotonic seconds-resolution clock. Default ``time.monotonic``."""


class TTLCache(Generic[T]):
    """Per-key TTL cache with eviction-on-read.

    Not LRU-bounded — Kora's expected keyspace is O(workspaces) which
    is O(1)-to-O(low-tens) per process. If that assumption changes we
    swap to ``cachetools.TTLCache``; flagged.
    """

    def __init__(
        self,
        ttl_seconds: float = 60.0,
        *,
        clock: Optional[ClockFn] = None,
    ):
        self._ttl = ttl_seconds
        self._clock: ClockFn = clock or time.monotonic
        self._entries: Dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> Optional[T]:
        """Return cached value if present + within TTL, else ``None``.

        Eviction-on-read: expired entries are dropped on access. Idle
        keys are not actively swept (keyspace is small).
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        fetched_at, value = entry
        if self._clock() - fetched_at >= self._ttl:
            # Expired — evict + miss.
            self._entries.pop(key, None)
            return None
        return value

    def put(self, key: str, value: T) -> None:
        """Store ``value`` under ``key`` with the current clock as the timestamp."""
        self._entries[key] = (self._clock(), value)

    def invalidate(self, key: str) -> None:
        """Drop a key. Idempotent."""
        self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop all keys. Test escape hatch + shutdown helper."""
        self._entries.clear()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def __contains__(self, key: str) -> bool:
        """``key in cache`` — True iff present + within TTL (no eviction side effect)."""
        entry = self._entries.get(key)
        if entry is None:
            return False
        fetched_at, _ = entry
        return self._clock() - fetched_at < self._ttl

    def __len__(self) -> int:
        return len(self._entries)
