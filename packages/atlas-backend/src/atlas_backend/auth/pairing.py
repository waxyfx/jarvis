"""Device enrolment.

A pairing code authorises exactly one enrolment. The *initiator* — an already
trusted device, or the bootstrap token for the very first pairing — decides in
advance what kind of device the code may enrol and under what name. A leaked
code therefore cannot be used to register, say, a second Windows agent when an
iPhone was intended.

The enrolling device proves possession of its private key by signing the code
together with its own public key, which also stops a man in the middle from
swapping in a key of their own.
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
from atlas_backend.db.models import Device, PairingCode
from atlas_shared.auth import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    normalise_pairing_code,
    pairing_code_hash,
    pairing_signing_input,
)
from atlas_shared.crypto import KEY_SIZE, verify
from atlas_shared.enums import DeviceKind, TrustLevel
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

__all__ = ["PairingService", "StartedPairing", "format_pairing_code"]

_CODE_COLLISION_RETRIES = 5


@dataclass(frozen=True, slots=True)
class StartedPairing:
    code: str
    expires_at: datetime


def format_pairing_code(code: str) -> str:
    """Group a code for display: ``4F2K9X1M`` -> ``4F2K-9X1M``."""
    midpoint = PAIRING_CODE_LENGTH // 2
    return f"{code[:midpoint]}-{code[midpoint:]}"


class PairingService:
    def __init__(self, settings: Settings) -> None:
        self._ttl = timedelta(seconds=settings.pairing_code_ttl_s)

    async def start(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        kind: DeviceKind,
        name: str,
        issued_by_device_id: uuid.UUID | None = None,
    ) -> StartedPairing:
        expires_at = utc_now() + self._ttl

        for _ in range(_CODE_COLLISION_RETRIES):
            code = "".join(
                secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH)
            )
            code_hash = pairing_code_hash(code)
            existing = (
                await session.execute(
                    select(PairingCode.id).where(PairingCode.code_hash == code_hash)
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue

            session.add(
                PairingCode(
                    user_id=user_id,
                    code_hash=code_hash,
                    intended_kind=kind.value,
                    intended_name=name,
                    issued_by_device_id=issued_by_device_id,
                    expires_at=expires_at,
                )
            )
            await session.flush()
            return StartedPairing(code=code, expires_at=expires_at)

        raise AtlasProtocolError(ErrorCode.INTERNAL, "could not allocate a pairing code")

    async def complete(
        self,
        session: AsyncSession,
        *,
        code: str,
        public_key: bytes,
        signature: bytes,
    ) -> Device:
        """Enrol a device against a pairing code.

        Raises:
            AtlasProtocolError: ``UNAUTHORIZED`` for a bad, spent or expired
                code, deliberately without distinguishing between them.
        """
        try:
            normalised = normalise_pairing_code(code)
        except ValueError as exc:
            raise AtlasProtocolError(ErrorCode.MALFORMED, str(exc)) from exc

        if len(public_key) != KEY_SIZE:
            raise AtlasProtocolError(ErrorCode.MALFORMED, f"public key must be {KEY_SIZE} bytes")

        pairing = (
            await session.execute(
                select(PairingCode)
                .where(PairingCode.code_hash == pairing_code_hash(normalised))
                .with_for_update()
            )
        ).scalar_one_or_none()

        if pairing is None:
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "pairing failed")
        if pairing.consumed_at is not None:
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "pairing failed")
        if pairing.expires_at <= utc_now():
            raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "pairing failed")

        if not verify(public_key, pairing_signing_input(normalised, public_key), signature):
            raise AtlasProtocolError(ErrorCode.SIGNATURE_INVALID, "pairing proof rejected")

        already_enrolled = (
            await session.execute(select(Device.id).where(Device.public_key == public_key))
        ).scalar_one_or_none()
        if already_enrolled is not None:
            raise AtlasProtocolError(
                ErrorCode.FORBIDDEN, "this key is already registered to a device"
            )

        device = Device(
            user_id=pairing.user_id,
            kind=pairing.intended_kind,
            name=pairing.intended_name,
            public_key=public_key,
            trust_level=TrustLevel.TRUSTED.value,
            capabilities=[],
        )
        session.add(device)
        pairing.consumed_at = utc_now()
        await session.flush()
        return device
