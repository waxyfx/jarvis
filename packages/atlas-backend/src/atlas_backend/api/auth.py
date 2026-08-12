"""Challenge/response authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from atlas_backend.api.schemas import (
    ChallengeRequest,
    ChallengeResponse,
    TokenRequest,
    TokenResponse,
)
from atlas_backend.audit import AuditActor, AuditEvent, append, append_detached
from atlas_backend.auth.challenge import ChallengeService, load_active_device
from atlas_backend.auth.deps import DbSession, get_challenge_service, get_token_service
from atlas_backend.auth.tokens import TokenService
from atlas_backend.db.base import utc_now
from atlas_shared.crypto import b64u_encode
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

router = APIRouter(prefix="/auth", tags=["auth"])


def _enforce_rate_limit(request: Request, device_id: str) -> None:
    limiter = request.app.state.auth_limiter
    if not limiter.allow(device_id):
        raise AtlasProtocolError(ErrorCode.RATE_LIMITED, "too many authentication attempts")


@router.post("/challenge", response_model=ChallengeResponse)
async def issue_challenge(
    body: ChallengeRequest,
    request: Request,
    session: DbSession,
    service: Annotated[ChallengeService, Depends(get_challenge_service)],
) -> ChallengeResponse:
    """Issue a single-use nonce for the device to sign."""
    _enforce_rate_limit(request, str(body.device_id))

    device = await load_active_device(session, body.device_id)
    challenge = await service.issue(session, device)

    await append(
        session,
        actor=AuditActor.DEVICE,
        event_type=AuditEvent.TOKEN_CHALLENGE_ISSUED,
        device_id=device.id,
    )
    return ChallengeResponse(nonce=b64u_encode(challenge.nonce), expires_at=challenge.expires_at)


@router.post("/token", response_model=TokenResponse)
async def issue_token(
    body: TokenRequest,
    request: Request,
    session: DbSession,
    challenges: Annotated[ChallengeService, Depends(get_challenge_service)],
    tokens: Annotated[TokenService, Depends(get_token_service)],
) -> TokenResponse:
    """Exchange a signed challenge for a short-lived access token."""
    _enforce_rate_limit(request, str(body.device_id))

    device = await load_active_device(session, body.device_id)

    try:
        await challenges.redeem(
            session,
            device=device,
            nonce=body.nonce_bytes,
            signature=body.signature_bytes,
        )
    except AtlasProtocolError as exc:
        # Detached, for the same reason as a failed pairing: a refused token is
        # a security event and must not vanish with the rolled-back request.
        await append_detached(
            request.app.state.database,
            actor=AuditActor.DEVICE,
            event_type=AuditEvent.TOKEN_REJECTED,
            device_id=device.id,
            payload={"reason": exc.code.value},
        )
        raise

    issued = tokens.issue(device)
    device.last_seen_at = utc_now()

    await append(
        session,
        actor=AuditActor.DEVICE,
        event_type=AuditEvent.TOKEN_ISSUED,
        device_id=device.id,
        payload={"token_id": issued.claims.token_id},
    )

    return TokenResponse(
        access_token=issued.token,
        expires_at=issued.expires_at,
        expires_in=int((issued.expires_at - utc_now()).total_seconds()),
    )
