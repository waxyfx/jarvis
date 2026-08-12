"""The wire envelope and its signature scheme.

Every ATLAS message — in any direction, over any transport — is an ``Envelope``.
Commands travelling towards a device that can act on the physical machine are
additionally signed, so that a compromised transport cannot inject one.

See docs/protocol.md for the narrative description.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atlas_shared.canonical import canonical_sha256_hex
from atlas_shared.crypto import b64u_decode, b64u_encode, sign, verify
from atlas_shared.enums import MessageKind
from atlas_shared.ids import is_ulid, new_ulid

__all__ = [
    "PROTOCOL_VERSION",
    "Envelope",
    "format_timestamp",
    "sign_envelope",
    "signing_input",
    "verify_envelope",
]

#: Bumped only for breaking changes. Peers refuse to talk across a mismatch.
PROTOCOL_VERSION = 1

#: Domain separation: a signature over an envelope can never be replayed as a
#: signature over anything else ATLAS signs.
_SIGNING_DOMAIN = b"atlas.envelope.v1"

#: ASCII unit separator. None of the signed fields can contain it (ULIDs,
#: timestamps and dotted type names are all restricted alphabets), so joining
#: with it makes the pre-image unambiguous.
_FIELD_SEPARATOR = b"\x1f"

_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+$")

UlidStr = Annotated[str, Field(min_length=26, max_length=26)]


def format_timestamp(value: datetime) -> str:
    """Canonical timestamp form: UTC, millisecond precision, ``Z`` suffix.

    Signature verification compares the *string*, so both peers must format
    identically; this is the single place that decides how.
    """
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Envelope(BaseModel):
    """A single protocol message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: int = Field(default=PROTOCOL_VERSION, ge=1)
    id: UlidStr = Field(default_factory=new_ulid)
    corr_id: UlidStr | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: MessageKind
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    sig: str | None = None

    @field_validator("id", "corr_id")
    @classmethod
    def _check_ulid(cls, value: str | None) -> str | None:
        if value is not None and not is_ulid(value):
            raise ValueError(f"not a canonical ULID: {value!r}")
        return value

    @field_validator("ts")
    @classmethod
    def _require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if not _TYPE_PATTERN.match(value):
            raise ValueError(f"invalid message type: {value!r}")
        return value

    def to_json(self) -> str:
        """Serialise for transmission."""
        data = self.model_dump(mode="json", exclude_none=True)
        data["ts"] = format_timestamp(self.ts)
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def signing_input(envelope: Envelope) -> bytes:
    """Bytes that a signature covers.

    The payload enters as a hash of its canonical encoding rather than inline,
    so the pre-image stays small and the framing cannot be confused by payload
    content.
    """
    fields = (
        _SIGNING_DOMAIN,
        str(envelope.v).encode("ascii"),
        envelope.id.encode("ascii"),
        (envelope.corr_id or "").encode("ascii"),
        format_timestamp(envelope.ts).encode("ascii"),
        envelope.kind.value.encode("ascii"),
        envelope.type.encode("ascii"),
        canonical_sha256_hex(envelope.payload).encode("ascii"),
    )
    return _FIELD_SEPARATOR.join(fields)


def sign_envelope(envelope: Envelope, private_key: bytes) -> Envelope:
    """Return a copy of ``envelope`` carrying a signature."""
    signature = sign(private_key, signing_input(envelope))
    return envelope.model_copy(update={"sig": b64u_encode(signature)})


def verify_envelope(envelope: Envelope, public_key: bytes) -> bool:
    """Whether ``envelope`` carries a valid signature by ``public_key``."""
    if envelope.sig is None:
        return False
    try:
        signature = b64u_decode(envelope.sig)
    except ValueError:
        return False
    return verify(public_key, signing_input(envelope), signature)
