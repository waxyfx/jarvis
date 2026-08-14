"""Device enrolment endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func, select

from atlas_backend.api.schemas import (
    PairCompleteRequest,
    PairCompleteResponse,
    PairStartRequest,
    PairStartResponse,
)
from atlas_backend.audit import AuditActor, AuditEvent, append, append_detached
from atlas_backend.auth.deps import (
    DbSession,
    PairingAuthority,
    SettingsDep,
    get_pairing_service,
)
from atlas_backend.auth.pairing import PairingService, format_pairing_code
from atlas_backend.db.models import Device, User
from atlas_shared.enums import DeviceKind, TrustLevel
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

router = APIRouter(prefix="/pair", tags=["pairing"])


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


def _enforce_rate_limit(request: Request) -> None:
    limiter = request.app.state.pairing_limiter
    if not limiter.allow(_client_key(request)):
        raise AtlasProtocolError(ErrorCode.RATE_LIMITED, "too many pairing attempts")


@router.post("/start", response_model=PairStartResponse, status_code=status.HTTP_201_CREATED)
async def start_pairing(
    body: PairStartRequest,
    request: Request,
    session: DbSession,
    settings: SettingsDep,
    initiator: PairingAuthority,
    service: Annotated[PairingService, Depends(get_pairing_service)],
) -> PairStartResponse:
    """Issue a single-use pairing code.

    Authorised either by an already trusted device or, only while no device
    exists at all, by the bootstrap token.
    """
    _enforce_rate_limit(request)

    if initiator is not None:
        user_id = initiator.user_id
    else:
        # Bootstrap path: create the owner record on first use.
        user = (await session.execute(select(User).limit(1))).scalar_one_or_none()
        if user is None:
            user = User(
                display_name=settings.owner_display_name,
                primary_lang=settings.owner_language,
                timezone=settings.owner_timezone,
            )
            session.add(user)
            await session.flush()
        user_id = user.id

    started = await service.start(
        session,
        user_id=user_id,
        kind=body.kind,
        name=body.name,
        issued_by_device_id=initiator.id if initiator else None,
    )

    await append(
        session,
        actor=AuditActor.DEVICE if initiator else AuditActor.SYSTEM,
        event_type=AuditEvent.PAIRING_STARTED,
        device_id=initiator.id if initiator else None,
        payload={
            "intended_kind": body.kind.value,
            "intended_name": body.name,
            "via": "device" if initiator else "bootstrap_token",
        },
    )

    return PairStartResponse(
        code=started.code,
        code_display=format_pairing_code(started.code),
        expires_at=started.expires_at,
    )


@router.post("/complete", response_model=PairCompleteResponse, status_code=status.HTTP_201_CREATED)
async def complete_pairing(
    body: PairCompleteRequest,
    request: Request,
    session: DbSession,
    service: Annotated[PairingService, Depends(get_pairing_service)],
) -> PairCompleteResponse:
    """Enrol a device that holds a valid pairing code and proves key possession."""
    _enforce_rate_limit(request)

    try:
        device = await service.complete(
            session,
            code=body.code,
            public_key=body.public_key_bytes,
            signature=body.signature_bytes,
        )
    except AtlasProtocolError as exc:
        # Detached: this request's transaction is about to roll back, and a
        # rejected pairing is precisely the kind of event that must survive it.
        await append_detached(
            request.app.state.database,
            actor=AuditActor.SYSTEM,
            event_type=AuditEvent.PAIRING_FAILED,
            payload={"reason": exc.code.value, "remote_addr": _client_key(request)},
        )
        raise

    await append(
        session,
        actor=AuditActor.DEVICE,
        event_type=AuditEvent.PAIRING_COMPLETED,
        device_id=device.id,
        payload={"kind": device.kind, "name": device.name},
    )

    return PairCompleteResponse(
        device_id=device.id,
        user_id=device.user_id,
        kind=DeviceKind(device.kind),
        name=device.name,
        trust_level=TrustLevel(device.trust_level),
        # Handed over exactly once, during enrolment, over the same channel that
        # already proved possession of the pairing code.
        server_public_key=request.app.state.server_identity.public_key_b64,
    )


@router.get("/status", tags=["pairing"])
async def pairing_status(session: DbSession) -> dict[str, object]:
    """Whether bootstrap enrolment is still open.

    Lets the agent's first-run flow tell "not paired yet" from "already set up"
    without guessing.
    """
    device_count = (await session.execute(select(func.count()).select_from(Device))).scalar_one()
    return {"devices": device_count, "bootstrap_open": device_count == 0}
