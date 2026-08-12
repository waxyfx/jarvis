"""Device inventory and revocation.

Revocation is the emergency brake described in PHASE-0 §10.4: it takes effect
immediately, both for future requests and for any connection already open.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from sqlalchemy import select

from atlas_backend.api.schemas import DeviceOut
from atlas_backend.audit import AuditActor, AuditEvent, append
from atlas_backend.auth.deps import DbSession, TrustedDevice
from atlas_backend.db.base import utc_now
from atlas_backend.db.models import Device
from atlas_shared.enums import TrustLevel
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("", response_model=list[DeviceOut])
async def list_devices(
    request: Request, session: DbSession, caller: TrustedDevice
) -> list[DeviceOut]:
    hub = request.app.state.hub
    devices = (
        (await session.execute(select(Device).where(Device.user_id == caller.user_id)))
        .scalars()
        .all()
    )
    return [
        DeviceOut.model_validate(device).model_copy(
            update={"connected": hub.is_connected(device.id)}
        )
        for device in devices
    ]


@router.post("/{device_id}/revoke", response_model=DeviceOut)
async def revoke_device(
    device_id: uuid.UUID,
    request: Request,
    session: DbSession,
    caller: TrustedDevice,
) -> DeviceOut:
    """Revoke one device. Terminal: re-pair to restore access."""
    device = (
        await session.execute(select(Device).where(Device.id == device_id).with_for_update())
    ).scalar_one_or_none()
    if device is None or device.user_id != caller.user_id:
        raise AtlasProtocolError(ErrorCode.FORBIDDEN, "unknown device")

    if device.is_active:
        device.trust_level = TrustLevel.REVOKED.value
        device.revoked_at = utc_now()
        await append(
            session,
            actor=AuditActor.USER,
            event_type=AuditEvent.DEVICE_REVOKED,
            device_id=device.id,
            payload={"revoked_by": str(caller.id), "scope": "single"},
        )

    await request.app.state.hub.disconnect(device.id, reason="revoked")
    return DeviceOut.model_validate(device).model_copy(update={"connected": False})


@router.post("/revoke-all", response_model=list[DeviceOut])
async def revoke_all_devices(
    request: Request, session: DbSession, caller: TrustedDevice
) -> list[DeviceOut]:
    """Emergency disconnect: revoke every device, including the caller.

    Recovery is by re-pairing with the bootstrap token, which is intentional —
    after a suspected compromise, everything should have to prove itself again.
    """
    devices = (
        (await session.execute(select(Device).where(Device.user_id == caller.user_id)))
        .scalars()
        .all()
    )
    revoked_now = utc_now()
    for device in devices:
        if device.is_active:
            device.trust_level = TrustLevel.REVOKED.value
            device.revoked_at = revoked_now

    await append(
        session,
        actor=AuditActor.USER,
        event_type=AuditEvent.DEVICE_REVOKED,
        device_id=caller.id,
        payload={"revoked_by": str(caller.id), "scope": "all", "count": len(devices)},
    )

    for device in devices:
        await request.app.state.hub.disconnect(device.id, reason="revoked")

    return [
        DeviceOut.model_validate(device).model_copy(update={"connected": False})
        for device in devices
    ]
