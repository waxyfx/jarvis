from datetime import UTC, datetime

import pytest

from atlas_shared.ids import ULID_LENGTH, is_ulid, new_ulid, ulid_timestamp


def test_new_ulid_is_canonical() -> None:
    value = new_ulid()
    assert len(value) == ULID_LENGTH
    assert is_ulid(value)
    assert value == value.upper()


def test_ulids_are_unique_across_a_batch() -> None:
    generated = {new_ulid() for _ in range(5_000)}
    assert len(generated) == 5_000


def test_ulids_generated_in_order_sort_in_order() -> None:
    earlier = new_ulid(timestamp_ms=1_700_000_000_000)
    later = new_ulid(timestamp_ms=1_700_000_001_000)
    assert earlier < later


def test_timestamp_round_trips() -> None:
    milliseconds = 1_786_000_123_456 % (1 << 48)
    value = new_ulid(timestamp_ms=milliseconds)
    recovered = ulid_timestamp(value)
    assert recovered == datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def test_leading_character_never_overflows() -> None:
    # A ULID is 128 bits rendered in 26 base32 chars (130 bits), so the first
    # character can only carry 3 bits. Verify the encoder respects that even at
    # the maximum representable timestamp.
    at_maximum = new_ulid(timestamp_ms=(1 << 48) - 1)
    assert at_maximum[0] in "01234567"
    assert is_ulid(at_maximum)


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "TOO-SHORT",
        "0" * 25,
        "0" * 27,
        "8" + "0" * 25,  # leading character overflows 128 bits
        "0" * 25 + "U",  # 'U' is excluded from Crockford base32
        new_ulid().lower(),  # lenient forms are deliberately rejected
        "0" * 25 + "I",  # Crockford alias for '1' — not accepted
    ],
)
def test_invalid_forms_are_rejected(candidate: str) -> None:
    assert not is_ulid(candidate)
    with pytest.raises(ValueError, match="not a canonical ULID"):
        ulid_timestamp(candidate)


def test_timestamp_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="out of ULID range"):
        new_ulid(timestamp_ms=1 << 48)
