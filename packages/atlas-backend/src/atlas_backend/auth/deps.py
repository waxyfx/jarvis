"""FastAPI dependencies for authentication and request scoping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.auth.challenge import ChallengeService, load_active_device
from atlas_backend.auth.pairing import PairingService
from atlas_backend.auth.tokens import TokenClaims, TokenService
from atlas_backend.config import Settings
from atlas_backend.db.models import Device
from atlas_backend.db.session import Database
from atlas_shared.crypto import constant_time_equals
from atlas_shared.enums import TrustLevel
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

__all__ = [
    "CurrentDevice",
    "DbSession",
    "PairingAuthority",
    "SettingsDep",
    "TrustedDevice",
    "get_challenge_service",
    "get_pairing_service",
    "get_token_service",
]


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


def get_token_service(request: Request) -> TokenService:
    service: TokenService = request.app.state.token_service
    return service


def get_challenge_service(request: Request) -> ChallengeService:
    service: ChallengeService = request.app.state.challenge_service
    return service


def get_pairing_service(request: Request) -> PairingService:
    service: PairingService = request.app.state.pairing_service
    return service


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """One transaction per request, committed when the handler returns cleanly.

    Uses the context manager directly rather than iterating a nested generator:
    when FastAPI closes this dependency, ``async with`` is guaranteed to unwind
    and return the connection to the pool. A nested ``async for`` would leave
    the inner generator for the garbage collector, leaking connections.
    """
    async with database.transaction() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbSession = Annotated[AsyncSession, Depends(get_session)]


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "missing credentials")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "missing credentials")
    return token.strip()


async def current_claims(
    token_service: Annotated[TokenService, Depends(get_token_service)],
    authorization: Annotated[str | None, Header()] = None,
) -> TokenClaims:
    return token_service.verify(_bearer_token(authorization))


async def current_device(
    claims: Annotated[TokenClaims, Depends(current_claims)],
    session: DbSession,
) -> Device:
    """Resolve the caller's device, re-checking revocation on every request.

    The token alone is not enough: a device revoked a minute ago still holds a
    valid-looking token until it expires, and revocation has to bite now.
    """
    return await load_active_device(session, claims.device_id)


CurrentDevice = Annotated[Device, Depends(current_device)]


async def trusted_device(device: CurrentDevice) -> Device:
    if device.trust_level != TrustLevel.TRUSTED.value:
        raise AtlasProtocolError(ErrorCode.FORBIDDEN, "device is not trusted")
    return device


TrustedDevice = Annotated[Device, Depends(trusted_device)]


async def pairing_authority(
    settings: SettingsDep,
    session: DbSession,
    token_service: Annotated[TokenService, Depends(get_token_service)],
    authorization: Annotated[str | None, Header()] = None,
    x_atlas_bootstrap_token: Annotated[str | None, Header()] = None,
) -> Device | None:
    """Authorise starting a pairing.

    Two ways in: an already trusted device, or — only while no device exists —
    the bootstrap token from the environment. The bootstrap path closes itself
    the moment the first device is enrolled, so a forgotten token in ``.env``
    stops being a way in.

    Returns:
        The initiating device, or ``None`` when the bootstrap token was used.
    """
    if authorization:
        claims = token_service.verify(_bearer_token(authorization))
        device = await load_active_device(session, claims.device_id)
        if device.trust_level != TrustLevel.TRUSTED.value:
            raise AtlasProtocolError(ErrorCode.FORBIDDEN, "device is not trusted")
        return device

    configured = settings.bootstrap_token
    if configured is None or not x_atlas_bootstrap_token:
        raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "missing credentials")

    from sqlalchemy import func, select

    device_count = (await session.execute(select(func.count()).select_from(Device))).scalar_one()
    if device_count > 0:
        raise AtlasProtocolError(
            ErrorCode.FORBIDDEN,
            "bootstrap pairing is closed; authorise from a paired device instead",
        )

    if not constant_time_equals(x_atlas_bootstrap_token, configured.get_secret_value()):
        raise AtlasProtocolError(ErrorCode.UNAUTHORIZED, "missing credentials")
    return None


PairingAuthority = Annotated[Device | None, Depends(pairing_authority)]
