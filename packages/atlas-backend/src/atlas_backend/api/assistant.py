"""The assistant endpoint: text in, action and answer out."""

from __future__ import annotations

import uuid
from datetime import UTC, date
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.ai import Assistant, StopReason, TurnResult
from atlas_backend.audit import AuditActor, AuditEvent, append
from atlas_backend.auth.deps import DbSession, TrustedDevice
from atlas_backend.db.base import utc_now
from atlas_backend.db.models import ApiUsageRow, Conversation, Device, Message
from atlas_shared.enums import Language
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

router = APIRouter(prefix="/assistant", tags=["assistant"])

_PROVIDER = "gemini"


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    language: Language = Language.RU
    #: Which agent should carry out any actions. Defaults to the caller.
    device_id: uuid.UUID | None = None


class ToolCallSummary(BaseModel):
    id: uuid.UUID
    tool: str
    risk: str
    decision: str
    status: str
    result: dict[str, Any] | None = None
    refusal: str | None = None


class RejectionSummary(BaseModel):
    tool: str
    reason: str
    detail: str


class MessageResponse(BaseModel):
    reply: str
    language: Language
    stopped_because: str
    iterations: int
    executed: list[ToolCallSummary]
    pending_confirmation: list[ToolCallSummary]
    denied: list[ToolCallSummary]
    rejected: list[RejectionSummary]


def _assistant(request: Request) -> Assistant:
    assistant: Assistant | None = request.app.state.assistant
    if assistant is None:
        raise AtlasProtocolError(
            ErrorCode.TOOL_NOT_IMPLEMENTED,
            "no language model is configured; set ATLAS_GEMINI_API_KEY on the backend",
        )
    return assistant


def _summarise(call: Any) -> ToolCallSummary:
    return ToolCallSummary(
        id=call.id,
        tool=call.tool_name,
        risk=call.risk_assessed,
        decision=call.decision,
        status=call.status,
        result=call.result,
        refusal=call.refusal,
    )


async def _open_conversation(
    session: AsyncSession, *, device: Device, language: Language
) -> Conversation:
    existing = (
        await session.execute(
            select(Conversation)
            .where(Conversation.user_id == device.user_id, Conversation.ended_at.is_(None))
            .order_by(Conversation.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    conversation = Conversation(
        user_id=device.user_id, origin_device_id=device.id, language=language.value
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def _check_budget(session: AsyncSession, limit: int) -> None:
    """Refuse before calling the model if today's allowance is spent."""
    today = utc_now().astimezone(UTC).date()
    row = (
        await session.execute(
            select(ApiUsageRow).where(ApiUsageRow.day == today, ApiUsageRow.provider == _PROVIDER)
        )
    ).scalar_one_or_none()

    if row is not None and row.input_tokens + row.output_tokens >= limit:
        raise AtlasProtocolError(
            ErrorCode.RATE_LIMITED,
            "the daily model token budget is exhausted; it resets at midnight UTC",
        )


async def _record_usage(session: AsyncSession, result: TurnResult, *, day: date) -> None:
    statement = (
        pg_insert(ApiUsageRow)
        .values(
            day=day,
            provider=_PROVIDER,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            calls=result.iterations,
        )
        .on_conflict_do_update(
            index_elements=[ApiUsageRow.day, ApiUsageRow.provider],
            set_={
                "input_tokens": ApiUsageRow.input_tokens + result.input_tokens,
                "output_tokens": ApiUsageRow.output_tokens + result.output_tokens,
                "calls": ApiUsageRow.calls + result.iterations,
            },
        )
    )
    await session.execute(statement)


@router.post("/message", response_model=MessageResponse)
async def send_message(
    body: MessageRequest,
    request: Request,
    session: DbSession,
    caller: TrustedDevice,
) -> MessageResponse:
    """Say something to ATLAS.

    The reply always reflects what really happened: an action that policy held
    for confirmation is reported as waiting, not as done.
    """
    assistant = _assistant(request)
    settings = request.app.state.settings

    target = caller
    if body.device_id is not None and body.device_id != caller.id:
        found = (
            await session.execute(select(Device).where(Device.id == body.device_id))
        ).scalar_one_or_none()
        if found is None or found.user_id != caller.user_id or not found.is_active:
            raise AtlasProtocolError(ErrorCode.FORBIDDEN, "unknown device")
        target = found

    await _check_budget(session, settings.ai_daily_token_budget)

    conversation = await _open_conversation(session, device=caller, language=body.language)
    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=body.text,
        language=body.language.value,
    )
    session.add(user_message)
    await session.flush()

    result = await assistant.handle(
        session,
        target=target,
        requester=caller,
        text=body.text,
        language=body.language,
        message_id=user_message.id,
    )

    session.add(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content=result.reply,
            language=result.language.value,
            token_usage={
                "input": result.input_tokens,
                "output": result.output_tokens,
            },
            stopped_because=result.stopped_because,
        )
    )
    await _record_usage(session, result, day=utc_now().astimezone(UTC).date())

    if result.stopped_because == StopReason.BUDGET_EXHAUSTED:
        await append(
            session,
            actor=AuditActor.SYSTEM,
            event_type=AuditEvent.AI_BUDGET_EXHAUSTED,
            device_id=target.id,
        )

    return MessageResponse(
        reply=result.reply,
        language=result.language,
        stopped_because=result.stopped_because,
        iterations=result.iterations,
        executed=[_summarise(call) for call in result.executed],
        pending_confirmation=[_summarise(call) for call in result.pending],
        denied=[_summarise(call) for call in result.denied],
        rejected=[
            RejectionSummary(tool=item.tool, reason=item.reason, detail=item.detail)
            for item in result.rejected
        ],
    )
