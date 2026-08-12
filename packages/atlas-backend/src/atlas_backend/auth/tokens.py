"""Access tokens.

Short-lived bearer tokens issued after a device proves possession of its private
key. There are deliberately **no refresh tokens**: the device's Ed25519 key is
already a long-lived credential held in hardware-backed storage, so a refresh
token would add a second, weaker secret to steal without adding capability.
Re-authenticating costs one challenge round trip every 15 minutes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from atlas_backend.config import Settings
from atlas_backend.db.models import Device
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

__all__ = ["IssuedToken", "TokenClaims", "TokenService"]

_ALGORITHM = "HS256"
_ISSUER = "atlas-backend"
_AUDIENCE = "atlas-device"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    device_id: uuid.UUID
    user_id: uuid.UUID
    device_kind: str
    trust_level: str
    token_id: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    expires_at: datetime
    claims: TokenClaims


class TokenService:
    def __init__(self, settings: Settings) -> None:
        self._secret = settings.jwt_secret.get_secret_value()
        self._ttl = timedelta(seconds=settings.access_token_ttl_s)
        self._leeway = timedelta(seconds=settings.clock_skew_tolerance_s)

    def issue(self, device: Device) -> IssuedToken:
        now = datetime.now(UTC)
        expires_at = now + self._ttl
        token_id = new_ulid()

        claims = TokenClaims(
            device_id=device.id,
            user_id=device.user_id,
            device_kind=device.kind,
            trust_level=device.trust_level,
            token_id=token_id,
            issued_at=now,
            expires_at=expires_at,
        )
        token = jwt.encode(
            {
                "iss": _ISSUER,
                "aud": _AUDIENCE,
                "sub": str(device.id),
                "uid": str(device.user_id),
                "knd": device.kind,
                "trt": device.trust_level,
                "jti": token_id,
                "iat": int(now.timestamp()),
                "exp": int(expires_at.timestamp()),
            },
            self._secret,
            algorithm=_ALGORITHM,
        )
        return IssuedToken(token=token, expires_at=expires_at, claims=claims)

    def verify(self, token: str) -> TokenClaims:
        """Decode and validate a bearer token.

        Raises:
            AtlasProtocolError: ``UNAUTHORIZED`` for every failure mode. The
                reason is deliberately not echoed to the caller.
        """
        try:
            decoded = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],
                audience=_AUDIENCE,
                issuer=_ISSUER,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "uid", "jti", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "token rejected") from exc

        try:
            return TokenClaims(
                device_id=uuid.UUID(decoded["sub"]),
                user_id=uuid.UUID(decoded["uid"]),
                device_kind=decoded["knd"],
                trust_level=decoded["trt"],
                token_id=decoded["jti"],
                issued_at=datetime.fromtimestamp(decoded["iat"], tz=UTC),
                expires_at=datetime.fromtimestamp(decoded["exp"], tz=UTC),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "token rejected") from exc
