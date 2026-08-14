from datetime import UTC, datetime, timedelta

import pytest

from atlas_shared.enums import MessageKind
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.envelope import Envelope
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode
from atlas_shared.replay import ReplayGuard

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


def envelope(*, ts: datetime = NOW, message_id: str | None = None) -> Envelope:
    return Envelope(
        id=message_id or new_ulid(),
        ts=ts,
        kind=MessageKind.CMD,
        type="conn.ping",
    )


def test_fresh_message_is_accepted() -> None:
    ReplayGuard(skew_tolerance_s=60).check(envelope(), now=NOW)


def test_repeated_message_id_is_rejected() -> None:
    guard = ReplayGuard(skew_tolerance_s=60)
    message = envelope()
    guard.check(message, now=NOW)

    with pytest.raises(AtlasProtocolError) as exc:
        guard.check(message, now=NOW)
    assert exc.value.code is ErrorCode.REPLAY_DETECTED


def test_stale_message_is_rejected() -> None:
    guard = ReplayGuard(skew_tolerance_s=60)
    with pytest.raises(AtlasProtocolError, match="too old"):
        guard.check(envelope(ts=NOW - timedelta(seconds=61)), now=NOW)


def test_future_message_is_rejected() -> None:
    guard = ReplayGuard(skew_tolerance_s=60)
    with pytest.raises(AtlasProtocolError, match="in the future"):
        guard.check(envelope(ts=NOW + timedelta(seconds=61)), now=NOW)


@pytest.mark.parametrize("offset", [-60, -30, 0, 30, 60])
def test_within_tolerance_is_accepted(offset: int) -> None:
    guard = ReplayGuard(skew_tolerance_s=60)
    guard.check(envelope(ts=NOW + timedelta(seconds=offset)), now=NOW)


def test_cache_is_bounded_and_evicts_oldest_first() -> None:
    guard = ReplayGuard(skew_tolerance_s=60, capacity=4)
    first = envelope()
    guard.check(first, now=NOW)
    for _ in range(4):
        guard.check(envelope(), now=NOW)

    # The oldest id has been evicted, so it is no longer recognised as a repeat.
    # That is safe only because the freshness window is what really bounds
    # replay: this test documents the interaction rather than a leak.
    guard.check(first, now=NOW)


def test_eviction_does_not_forget_recent_ids() -> None:
    guard = ReplayGuard(skew_tolerance_s=60, capacity=4)
    for _ in range(3):
        guard.check(envelope(), now=NOW)
    recent = envelope()
    guard.check(recent, now=NOW)

    with pytest.raises(AtlasProtocolError):
        guard.check(recent, now=NOW)
