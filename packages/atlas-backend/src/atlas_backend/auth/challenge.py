"""Challenge/response device authentication.

The server issues a random nonce; the device signs it with the private key that
matches its registered public key. The nonce is server-generated so a device
never chooses what it signs, and single-use so a captured signature is worthless
the moment it is spent.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.config import Settings
from atlas_backend.db.base import utc_now
from atlas_backend.db.models import AuthChallenge, Device
from atlas_shared.auth import challenge_signing_input
from atlas_shared.crypto import verify
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

__all__ = ["ChallengeService", "IssuedChallenge"]

_NONCE_BYTES = 32


@dataclass(frozen=True, slots=True)
class IssuedChallenge:
    nonce: bytes
    expires_at: datetime


class ChallengeService:
    def __init__(self, settings: Settings) -> None:
        self._ttl = timedelta(seconds=settings.challenge_ttl_s)

    async def issue(self, session: AsyncSession, device: Device) -> IssuedChallenge:
        challenge = AuthChallenge(
            device_id=device.id,
            nonce=secrets.token_bytes(_NONCE_BYTES),
            expires_at=utc_now() + self._ttl,
        )
        session.add(challenge)
        await session.flush()
        return IssuedChallenge(nonce=challenge.nonce, expires_at=challenge.expires_at)

    async def redeem(
        self,
        session: AsyncSession,
        *,
        device: Device,
        nonce: bytes,
        signature: bytes,
    ) -> None:
        """Consume a challenge, verifying the device's signature over it.

        Raises:
            AtlasProtocolError: ``UNAUTHORIZED`` on any failure. Callers must not
                surface which check failed — that would let an attacker probe
                whether a given nonce exists.
        """
        challenge = (
            await session.execute(
                select(AuthChallenge).where(AuthChallenge.nonce == nonce).with_for_update()
            )
        ).scalar_one_or_none()

        if challenge is None:
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "authentication failed")
        if challenge.device_id != device.id:
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "authentication failed")
        if challenge.consumed_at is not None:
            raise AtlasProtocolError(ErrorCode.REPLAY_DETECTED, "challenge already used")
        if challenge.expires_at <= utc_now():
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "authentication failed")

        expected = challenge_signing_input(str(device.id), nonce)
        if not verify(device.public_key, expected, signature):
            raise AtlasProtocolError(ErrorCode.SIGNATURE_INVALID, "authentication failed")

        # Marked spent only after the signature verifies, so a failed attempt
        # does not burn a legitimate device's challenge.
        challenge.consumed_at = utc_now()
        await session.flush()

    async def purge_expired(self, session: AsyncSession, *, before: datetime | None = None) -> int:
        """Drop spent and expired challenges. Returns the number removed."""
        cutoff = before or utc_now()
        stale = (
            (await session.execute(select(AuthChallenge).where(AuthChallenge.expires_at < cutoff)))
            .scalars()
            .all()
        )
        for challenge in stale:
            await session.delete(challenge)
        return len(stale)


async def load_active_device(session: AsyncSession, device_id: uuid.UUID) -> Device:
    """Fetch a device, refusing unknown and revoked ones alike."""
    device = (
        await session.execute(select(Device).where(Device.id == device_id))
    ).scalar_one_or_none()
    if device is None or not device.is_active:
        raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "authentication failed")
    return device
