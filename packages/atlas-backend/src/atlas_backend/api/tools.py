"""Requesting tool execution, and confirming what policy held back."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from atlas_backend.auth.deps import DbSession, TrustedDevice
from atlas_backend.db.models import Device, ToolCall
from atlas_backend.policy import ToolDispatcher
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode
from atlas_shared.tools.catalog import CATALOG
from atlas_shared.tools.manifest import ToolDescriptor

router = APIRouter(prefix="/tools", tags=["tools"])


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Which agent should run it. Defaults to the caller's own device.
    device_id: uuid.UUID | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class ToolCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool_name: str
    risk_assessed: str
    decision: str
    policy_reasons: list[str]
    status: str
    risk_local: str | None
    refusal: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    duration_ms: int | None


def _dispatcher(request: Request) -> ToolDispatcher:
    dispatcher: ToolDispatcher = request.app.state.dispatcher
    return dispatcher


async def _target_device(session: DbSession, caller: Device, device_id: uuid.UUID | None) -> Device:
    if device_id is None or device_id == caller.id:
        return caller
    device = (
        await session.execute(select(Device).where(Device.id == device_id))
    ).scalar_one_or_none()
    if device is None or device.user_id != caller.user_id or not device.is_active:
        raise AtlasProtocolError(ErrorCode.FORBIDDEN, "unknown device")
    return device


@router.get("", response_model=list[ToolDescriptor])
async def list_tools(_caller: TrustedDevice) -> list[ToolDescriptor]:
    """The declared catalogue, with argument schemas and risk classes.

    The same descriptors that will be handed to the language model in M3, so
    what the model can see and what policy enforces come from one source.
    """
    return list(CATALOG.descriptors())


@router.post("/{tool_name}/execute", response_model=ToolCallOut)
async def execute_tool(
    tool_name: str,
    body: ExecuteRequest,
    request: Request,
    session: DbSession,
    caller: TrustedDevice,
) -> ToolCallOut:
    """Run a tool, subject to policy.

    A response is returned in every case — allowed and executed, held for
    confirmation, denied, or refused by the agent. The ``status`` and
    ``policy_reasons`` fields say which.
    """
    target = await _target_device(session, caller, body.device_id)
    outcome = await _dispatcher(request).execute(
        session,
        target=target,
        requester=caller,
        tool_name=tool_name,
        args=body.args,
    )
    return ToolCallOut.model_validate(outcome.call)


@router.post("/calls/{call_id}/confirm", response_model=ToolCallOut)
async def confirm_call(
    call_id: uuid.UUID,
    request: Request,
    session: DbSession,
    caller: TrustedDevice,
) -> ToolCallOut:
    """Approve a call that policy held for confirmation, and run it."""
    outcome = await _dispatcher(request).confirm(session, call_id=call_id, confirmed_by=caller)
    return ToolCallOut.model_validate(outcome.call)


@router.get("/calls", response_model=list[ToolCallOut])
async def list_calls(
    session: DbSession, caller: TrustedDevice, limit: int = 50
) -> list[ToolCallOut]:
    rows = (
        (
            await session.execute(
                select(ToolCall)
                .where(ToolCall.device_id == caller.id)
                .order_by(ToolCall.created_at.desc())
                .limit(min(limit, 200))
            )
        )
        .scalars()
        .all()
    )
    return [ToolCallOut.model_validate(row) for row in rows]
