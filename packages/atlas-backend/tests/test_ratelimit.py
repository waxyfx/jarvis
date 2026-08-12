import pytest

from atlas_backend.ratelimit import SlidingWindowLimiter


def test_allows_up_to_the_limit() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_s=60.0)
    assert [limiter.allow("a", now=0.0) for _ in range(3)] == [True, True, True]


def test_blocks_beyond_the_limit() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_s=60.0)
    limiter.allow("a", now=0.0)
    limiter.allow("a", now=1.0)
    assert limiter.allow("a", now=2.0) is False


def test_window_slides() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_s=10.0)
    limiter.allow("a", now=0.0)
    limiter.allow("a", now=1.0)
    assert limiter.allow("a", now=5.0) is False
    # The first hit ages out at t=10, freeing one slot.
    assert limiter.allow("a", now=10.5) is True


def test_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_s=60.0)
    assert limiter.allow("a", now=0.0) is True
    assert limiter.allow("b", now=0.0) is True
    assert limiter.allow("a", now=0.0) is False


def test_reset_single_key_and_all() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_s=60.0)
    limiter.allow("a", now=0.0)
    limiter.allow("b", now=0.0)

    limiter.reset("a")
    assert limiter.allow("a", now=0.0) is True
    assert limiter.allow("b", now=0.0) is False

    limiter.reset()
    assert limiter.allow("b", now=0.0) is True


def test_zero_limit_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        SlidingWindowLimiter(limit=0, window_s=60.0)
