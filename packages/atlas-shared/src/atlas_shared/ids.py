"""ULID generation and validation.

ULIDs are used for every wire message and most database rows: they sort by
creation time (useful for audit ordering) while staying collision-safe without
coordination between devices.

Layout: 48-bit big-endian millisecond timestamp || 80 bits of CSPRNG randomness,
rendered as 26 Crockford base32 characters.

We generate and accept only canonical uppercase form. Crockford's lenient
aliases (I/L -> 1, O -> 0, lowercase) are deliberately *not* accepted, so that a
given ULID has exactly one valid representation and equality checks are safe.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

__all__ = ["ULID_LENGTH", "is_ulid", "new_ulid", "ulid_timestamp"]

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE: dict[str, int] = {char: index for index, char in enumerate(_ALPHABET)}

ULID_LENGTH = 26
_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80
_MAX_TIMESTAMP_MS = (1 << _TIMESTAMP_BITS) - 1


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Return a new ULID.

    Args:
        timestamp_ms: Milliseconds since the Unix epoch. Defaults to now.
            Exposed for deterministic tests, not for backdating real records.
    """
    milliseconds = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= milliseconds <= _MAX_TIMESTAMP_MS:
        raise ValueError(f"timestamp out of ULID range: {milliseconds}")

    value = (milliseconds << _RANDOM_BITS) | secrets.randbits(_RANDOM_BITS)
    return _encode(value)


def is_ulid(candidate: str) -> bool:
    """Whether ``candidate`` is a canonical ULID string."""
    if len(candidate) != ULID_LENGTH:
        return False
    # 26 base32 chars carry 130 bits but a ULID is 128, so the leading character
    # can only encode 3 bits. Anything above '7' would overflow on decode.
    if candidate[0] not in "01234567":
        return False
    return all(char in _DECODE for char in candidate)


def ulid_timestamp(ulid: str) -> datetime:
    """Extract the embedded creation time as a timezone-aware UTC datetime."""
    if not is_ulid(ulid):
        raise ValueError(f"not a canonical ULID: {ulid!r}")

    value = 0
    for char in ulid:
        value = (value << 5) | _DECODE[char]
    milliseconds = value >> _RANDOM_BITS
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def _encode(value: int) -> str:
    characters = [""] * ULID_LENGTH
    for position in range(ULID_LENGTH - 1, -1, -1):
        characters[position] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(characters)
