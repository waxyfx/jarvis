"""Request and response bodies for the REST surface.

Binary values (keys, nonces, signatures) travel as unpadded base64url strings
and are decoded here, so routers only ever see ``bytes``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atlas_shared.crypto import KEY_SIZE, SIGNATURE_SIZE, b64u_decode
from atlas_shared.enums import DeviceKind, TrustLevel

__all__ = [
    "AuditEntryOut",
    "ChallengeRequest",
    "ChallengeResponse",
    "DeviceOut",
    "HealthResponse",
    "PairCompleteRequest",
    "PairCompleteResponse",
    "PairStartRequest",
    "PairStartResponse",
    "ReadinessResponse",
    "ServerIdentityResponse",
    "TokenRequest",
    "TokenResponse",
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _decode_fixed(value: str, size: int, label: str) -> bytes:
    try:
        raw = b64u_decode(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not valid base64url") from exc
    if len(raw) != size:
        raise ValueError(f"{label} must decode to {size} bytes, got {len(raw)}")
    return raw


class HealthResponse(BaseModel):
    status: str
    version: str
    protocol_version: int


class ReadinessResponse(HealthResponse):
    database: str


class PairStartRequest(_Body):
    kind: DeviceKind
    name: Annotated[str, Field(min_length=1, max_length=120)]


class PairStartResponse(BaseModel):
    #: Canonical, unseparated form — what the enrolling device must sign.
    code: str
    #: Grouped for reading aloud and typing.
    code_display: str
    expires_at: datetime


class PairCompleteRequest(_Body):
    code: Annotated[str, Field(min_length=1, max_length=32)]
    public_key: str
    signature: str

    @field_validator("public_key")
    @classmethod
    def _check_public_key(cls, value: str) -> str:
        _decode_fixed(value, KEY_SIZE, "public_key")
        return value

    @field_validator("signature")
    @classmethod
    def _check_signature(cls, value: str) -> str:
        _decode_fixed(value, SIGNATURE_SIZE, "signature")
        return value

    @property
    def public_key_bytes(self) -> bytes:
        return b64u_decode(self.public_key)

    @property
    def signature_bytes(self) -> bytes:
        return b64u_decode(self.signature)


class PairCompleteResponse(BaseModel):
    device_id: uuid.UUID
    user_id: uuid.UUID
    kind: DeviceKind
    name: str
    trust_level: TrustLevel
    #: The device pins this and refuses commands signed by any other key.
    server_public_key: str


class ServerIdentityResponse(BaseModel):
    public_key: str
    algorithm: str = "ed25519"


class ChallengeRequest(_Body):
    device_id: uuid.UUID


class ChallengeResponse(BaseModel):
    nonce: str
    expires_at: datetime


class TokenRequest(_Body):
    device_id: uuid.UUID
    nonce: str
    signature: str

    @field_validator("signature")
    @classmethod
    def _check_signature(cls, value: str) -> str:
        _decode_fixed(value, SIGNATURE_SIZE, "signature")
        return value

    @property
    def nonce_bytes(self) -> bytes:
        return b64u_decode(self.nonce)

    @property
    def signature_bytes(self) -> bytes:
        return b64u_decode(self.signature)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - the scheme name, not a secret
    expires_at: datetime
    expires_in: int


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    name: str
    trust_level: str
    capabilities: list[str]
    last_seen_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None
    connected: bool = False


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    chain_index: int
    ts: datetime
    actor: str
    device_id: uuid.UUID | None
    event_type: str
    payload: dict[str, object]
