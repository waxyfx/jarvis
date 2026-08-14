"""Dispatch: policy decision, signed command, verified result, audit entry.

This is the path the whole permission model exists to protect, and every step is
recorded. A call that is denied, one that waits for confirmation, one that the
agent refuses and one that succeeds all leave a row in ``tool_calls`` and an
entry in the audit chain — so "nothing happened" is never indistinguishable from
"nothing was recorded".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.audit import AuditActor, AuditEvent, append
from atlas_backend.config import Settings
from atlas_backend.db.base import utc_now
from atlas_backend.db.models import Device, PermissionOverrideRow, ToolCall
from atlas_backend.logging import get_logger
from atlas_backend.policy.engine import (
    OverrideMode,
    PermissionOverride,
    PolicyRequest,
    decide,
)
from atlas_backend.server_identity import ServerIdentity
from atlas_backend.ws.hub import DeviceOfflineError, Hub
from atlas_shared.enums import AgentMode, Decision, ToolStatus, TrustLevel
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode
from atlas_shared.protocol.messages import ToolExecute, ToolResult, build_envelope
from atlas_shared.tools.catalog import CATALOG
from atlas_shared.tools.manifest import RiskContext

__all__ = ["CallStatus", "ToolDispatcher"]

log = get_logger(__name__)


class CallStatus:
    """Values of ``tool_calls.status``."""

    DENIED = "denied"
    PENDING_CONFIRMATION = "pending_confirmation"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    call: ToolCall
    result: ToolResult | None


class ToolDispatcher:
    def __init__(self, *, hub: Hub, server_identity: ServerIdentity, settings: Settings) -> None:
        self._hub = hub
        self._identity = server_identity
        self._settings = settings

    def risk_context(self) -> RiskContext:
        return RiskContext(
            allowed_roots=self._settings.allowed_file_roots,
            executable_roots=self._settings.allowed_executable_roots,
        )

    async def execute(
        self,
        session: AsyncSession,
        *,
        target: Device,
        requester: Device | None,
        tool_name: str,
        args: Mapping[str, Any],
        external_content_present: bool = False,
        message_id: uuid.UUID | None = None,
    ) -> DispatchOutcome:
        if not CATALOG.has(tool_name):
            raise AtlasProtocolError(
                ErrorCode.UNSUPPORTED_TYPE, f"unknown tool: {tool_name}", {"tool": tool_name}
            )
        manifest = CATALOG.get(tool_name)

        # Validate here as well as on the agent: a malformed request should fail
        # before it becomes a signed command.
        try:
            manifest.validate_args(args)
        except ValidationError as exc:
            raise AtlasProtocolError(
                ErrorCode.MALFORMED,
                f"invalid arguments for {tool_name}",
                {"errors": exc.errors(include_url=False, include_input=False)},
            ) from exc

        overrides = await self._load_overrides(session, target.user_id)
        connection = self._hub.get(target.id)
        agent_mode = connection.mode if connection is not None else AgentMode.NORMAL

        verdict = decide(
            PolicyRequest(
                tool=manifest,
                args=args,
                risk_context=self.risk_context(),
                device_trust=TrustLevel(target.trust_level),
                agent_mode=agent_mode,
                now=utc_now(),
                overrides=overrides,
                external_content_present=external_content_present,
            )
        )

        call = ToolCall(
            id=uuid.uuid4(),
            device_id=target.id,
            requested_by_device_id=requester.id if requester else None,
            tool_name=manifest.name,
            tool_version=manifest.version,
            args=dict(args),
            risk_assessed=verdict.risk.value,
            decision=verdict.decision.value,
            policy_reasons=list(verdict.reasons),
            status=CallStatus.DENIED,
            message_id=message_id,
        )
        session.add(call)
        await session.flush()

        if verdict.decision is Decision.DENY:
            await self._audit(session, call, AuditEvent.TOOL_DENIED)
            return DispatchOutcome(call=call, result=None)

        if verdict.decision is Decision.CONFIRM:
            call.status = CallStatus.PENDING_CONFIRMATION
            await self._audit(session, call, AuditEvent.TOOL_CONFIRMATION_REQUIRED)
            return DispatchOutcome(call=call, result=None)

        return await self.dispatch(session, call)

    async def dispatch(self, session: AsyncSession, call: ToolCall) -> DispatchOutcome:
        """Send an approved call to the agent and record what came back."""
        command = ToolExecute(
            call_id=str(call.id),
            tool=call.tool_name,
            tool_version=call.tool_version,
            args=call.args,
            risk=call.risk_assessed,  # type: ignore[arg-type]
            deadline_s=CATALOG.get(call.tool_name).timeout_s,
        )
        envelope = self._identity.sign(
            build_envelope("agent.tool.execute", command, corr_id=new_ulid())
        )

        call.status = CallStatus.DISPATCHED
        call.dispatched_at = utc_now()
        await self._audit(session, call, AuditEvent.TOOL_DISPATCHED)
        # Commit before waiting: the agent may answer in milliseconds, and the
        # result handler must not race an uncommitted row.
        await session.commit()

        try:
            answer = await self._hub.request(
                call.device_id, envelope, timeout_s=self._settings.tool_dispatch_timeout_s
            )
        except DeviceOfflineError:
            call.status = CallStatus.UNREACHABLE
            call.completed_at = utc_now()
            await self._audit(session, call, AuditEvent.TOOL_FAILED, {"reason": "device offline"})
            return DispatchOutcome(call=call, result=None)
        except TimeoutError:
            call.status = CallStatus.TIMEOUT
            call.completed_at = utc_now()
            await self._audit(session, call, AuditEvent.TOOL_FAILED, {"reason": "timeout"})
            return DispatchOutcome(call=call, result=None)

        result: ToolResult = answer
        call.status = CallStatus.COMPLETED
        call.completed_at = utc_now()
        call.duration_ms = result.duration_ms
        call.risk_local = result.risk_local.value if result.risk_local else None
        call.refusal = result.refusal.value if result.refusal else None
        call.result = result.result
        call.error = result.failure.model_dump(mode="json") if result.failure else None

        event = (
            AuditEvent.TOOL_EXECUTED
            if result.status is ToolStatus.OK
            else AuditEvent.TOOL_REFUSED
            if result.status is ToolStatus.REFUSED
            else AuditEvent.TOOL_FAILED
        )
        await self._audit(session, call, event, {"status": result.status.value})

        if call.risk_local and call.risk_local != call.risk_assessed:
            # Worth surfacing: the two sides derived different risk from the same
            # manifest, which means one of them is working from stale rules.
            log.warning(
                "risk_assessment_diverged",
                tool=call.tool_name,
                server=call.risk_assessed,
                agent=call.risk_local,
            )

        return DispatchOutcome(call=call, result=result)

    async def confirm(
        self, session: AsyncSession, *, call_id: uuid.UUID, confirmed_by: Device
    ) -> DispatchOutcome:
        """Approve a call that policy held for confirmation, then dispatch it."""
        call = (
            await session.execute(select(ToolCall).where(ToolCall.id == call_id).with_for_update())
        ).scalar_one_or_none()

        if call is None:
            raise AtlasProtocolError(ErrorCode.FORBIDDEN, "unknown call")
        if call.status != CallStatus.PENDING_CONFIRMATION:
            raise AtlasProtocolError(
                ErrorCode.FORBIDDEN, f"call is not awaiting confirmation (status: {call.status})"
            )
        if confirmed_by.trust_level != TrustLevel.TRUSTED.value:
            raise AtlasProtocolError(ErrorCode.FORBIDDEN, "device is not trusted")

        call.confirmed_by_device_id = confirmed_by.id
        call.confirmed_at = utc_now()
        await self._audit(session, call, AuditEvent.TOOL_CONFIRMED)
        return await self.dispatch(session, call)

    async def _load_overrides(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> tuple[PermissionOverride, ...]:
        rows = (
            (
                await session.execute(
                    select(PermissionOverrideRow).where(PermissionOverrideRow.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            PermissionOverride(
                tool_pattern=row.tool_pattern,
                mode=OverrideMode(row.mode),
                expires_at=row.expires_at,
            )
            for row in rows
        )

    async def _audit(
        self,
        session: AsyncSession,
        call: ToolCall,
        event: AuditEvent,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "call_id": str(call.id),
            "tool": call.tool_name,
            "risk": call.risk_assessed,
            "decision": call.decision,
            "reasons": call.policy_reasons,
        }
        if call.risk_local:
            payload["risk_local"] = call.risk_local
        if call.refusal:
            payload["refusal"] = call.refusal
        if extra:
            payload.update(extra)

        await append(
            session,
            actor=AuditActor.USER,
            event_type=event,
            device_id=call.device_id,
            payload=payload,
        )
