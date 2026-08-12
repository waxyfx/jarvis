"""A small in-process sliding-window limiter.

Scope note: this is per-process state. ATLAS runs as a single backend process
for a single user, so that is sufficient and honest. If the deployment ever
grows a second process, this must move to the database or a shared store —
until then, a distributed limiter would be complexity without a purpose.
"""

from __future__ import annotations

import time
from collections import deque

__all__ = ["SlidingWindowLimiter"]


class SlidingWindowLimiter:
    def __init__(self, *, limit: int, window_s: float) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record an attempt for ``key`` and report whether it is permitted."""
        current = time.monotonic() if now is None else now
        window_start = current - self._window

        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= window_start:
            hits.popleft()

        if len(hits) >= self._limit:
            return False

        hits.append(current)
        return True

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)
