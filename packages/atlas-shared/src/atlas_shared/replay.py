"""Replay protection, used identically by the backend and the agent.

Shared rather than duplicated: the two sides must agree on what "already seen"
and "too old" mean, and a divergence would be a security hole that no test on
either side alone would catch.

Two independent checks, because either alone is porous:

* **Freshness** — an envelope whose timestamp is far from local time is
  rejected, which bounds how long a captured frame stays useful.
* **Uniqueness** — a message id seen before is rejected, which stops replay
  inside the freshness window.

The id cache is bounded, and the freshness window is what makes that safe: an id
old enough to be evicted is already too old to pass the clock check.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from atlas_shared.protocol.envelope import Envelope
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

__all__ = ["ReplayGuard"]


class ReplayGuard:
    def __init__(self, *, skew_tolerance_s: int, capacity: int = 4096) -> None:
        self._tolerance = timedelta(seconds=skew_tolerance_s)
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def check(self, envelope: Envelope, *, now: datetime | None = None) -> None:
        """Raise if ``envelope`` is stale, from the future, or a repeat."""
        moment = now or datetime.now(UTC)
        age = moment - envelope.ts

        if age > self._tolerance:
            raise AtlasProtocolError(
                ErrorCode.REPLAY_DETECTED,
                "message timestamp is too old",
                {"age_s": round(age.total_seconds(), 3)},
            )
        if -age > self._tolerance:
            raise AtlasProtocolError(
                ErrorCode.REPLAY_DETECTED,
                "message timestamp is in the future",
                {"skew_s": round(-age.total_seconds(), 3)},
            )

        if envelope.id in self._seen:
            raise AtlasProtocolError(ErrorCode.REPLAY_DETECTED, "message id has already been used")

        self._seen[envelope.id] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

    def seen_count(self) -> int:
        return len(self._seen)
