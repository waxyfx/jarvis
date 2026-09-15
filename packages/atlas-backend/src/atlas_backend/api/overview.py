"""One call that answers "what is JARVIS doing?".

Written for the phone. A phone that had to make five requests to draw one screen
would make five requests on every foreground, on a connection that is sometimes
a train tunnel — so this is the five, gathered server-side, in the shape the
screen actually needs.

**It reads, and only reads.** Nothing here dispatches a tool, reaches the agent
or touches the tracker over HTTP: everything comes from rows already written, so
the answer is fast, costs nothing, and is the same whether the laptop is awake
or shut. The cost is that the telemetry is as fresh as the last thing the agent
sent, which is why the answer says how old it is rather than implying it is now.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.activity.store import samples_between, start_of_day
from atlas_backend.activity.summary import summarise
from atlas_backend.auth.deps import DbSession, TrustedDevice
from atlas_backend.db.base import utc_now
from atlas_backend.db.models import Device, SystemTelemetryRow, ToolCall
from atlas_backend.policy.service import CallStatus
from atlas_shared.enums import DeviceKind

router = APIRouter(prefix="/overview", tags=["overview"])

#: Telemetry older than this is reported but not treated as current. The agent
#: sends every sixty seconds, so five minutes means several were missed.
_STALE_AFTER = timedelta(minutes=5)


class MachineOut(BaseModel):
    """The Windows machine, as far as this backend last heard."""

    model_config = ConfigDict(from_attributes=True)

    device_id: uuid.UUID
    name: str
    #: Whether a websocket is open right now. The only field here that is
    #: certainly current.
    connected: bool
    last_seen_at: datetime | None
    #: None when the agent has never reported, or when what it reported is old
    #: enough that showing it as the present state would be a lie.
    cpu_pct: float | None = None
    ram_used_pct: float | None = None
    uptime_s: int | None = None
    telemetry_age_s: int | None = None
    telemetry_stale: bool = False


class PendingOut(BaseModel):
    """An action waiting for someone to say yes.

    ``id`` rather than ``call_id`` because that is what the assistant endpoint
    has called it since M3. Two endpoints naming the same thing differently is
    how a client ends up decoding one and failing on the other.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool: str
    risk: str
    requested_at: datetime


class OverviewOut(BaseModel):
    at: datetime
    machines: list[MachineOut]
    #: Held by the Policy Engine. The phone is a good place to answer these,
    #: which is most of why this endpoint exists.
    pending: list[PendingOut]
    #: Minutes at the machine today, from the samples already stored. Absent
    #: when nothing was recorded, which is not the same as zero.
    active_minutes_today: int | None = None


@router.get("", response_model=OverviewOut)
async def overview(request: Request, session: DbSession, caller: TrustedDevice) -> OverviewOut:
    """Everything a phone needs to draw its main screen, in one request."""
    now = utc_now()
    hub = request.app.state.hub

    machines = []
    agents = await session.execute(
        select(Device).where(
            Device.user_id == caller.user_id,
            Device.kind == DeviceKind.WINDOWS_AGENT.value,
            Device.revoked_at.is_(None),
        )
    )
    for device in agents.scalars():
        machines.append(await _machine(session, device, hub=hub, now=now))

    pending = await session.execute(
        select(ToolCall)
        .where(
            ToolCall.status == CallStatus.PENDING_CONFIRMATION,
            ToolCall.device_id.in_([machine.device_id for machine in machines]),
        )
        .order_by(ToolCall.created_at.desc())
        .limit(20)
    )

    return OverviewOut(
        at=now,
        machines=machines,
        pending=[
            PendingOut(
                id=call.id,
                tool=call.tool_name,
                risk=call.risk_assessed,
                requested_at=call.created_at,
            )
            for call in pending.scalars()
        ],
        active_minutes_today=await _active_minutes(session, machines, now=now),
    )


async def _machine(
    session: AsyncSession, device: Device, *, hub: object, now: datetime
) -> MachineOut:
    latest = await session.execute(
        select(SystemTelemetryRow)
        .where(SystemTelemetryRow.device_id == device.id)
        .order_by(SystemTelemetryRow.ts.desc())
        .limit(1)
    )
    telemetry = latest.scalar_one_or_none()

    machine = MachineOut(
        device_id=device.id,
        name=device.name,
        connected=hub.is_connected(device.id),  # type: ignore[attr-defined]
        last_seen_at=device.last_seen_at,
    )
    if telemetry is None:
        return machine

    age = int((now - telemetry.ts).total_seconds())
    stale = age > _STALE_AFTER.total_seconds()
    return machine.model_copy(
        update={
            # Reported either way, with its age. Hiding stale numbers loses the
            # only evidence of when the machine was last doing anything.
            "cpu_pct": telemetry.cpu_pct,
            "ram_used_pct": telemetry.ram_used_pct,
            "uptime_s": telemetry.uptime_s,
            "telemetry_age_s": age,
            "telemetry_stale": stale,
        }
    )


async def _active_minutes(
    session: AsyncSession, machines: list[MachineOut], *, now: datetime
) -> int | None:
    """Time at the machine today, or None when nothing was recorded.

    None rather than zero. "You worked no minutes today" is a claim about the
    day; "nothing was recorded" is a claim about the record, and only the second
    one is true when the agent was not running.
    """
    if not machines:
        return None

    device_id = machines[0].device_id
    samples = await samples_between(session, device_id=device_id, since=start_of_day(now))
    if not samples:
        return None
    return summarise(samples, now=now).active_minutes
