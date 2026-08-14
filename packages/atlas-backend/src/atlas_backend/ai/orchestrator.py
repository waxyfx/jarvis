"""The assistant turn: from what the user typed to what actually happened.

    user text → model → proposed tool calls → validation → Policy Engine
              → confirmation if required → signed command → agent → execution
              → signed result → reply

The model's only power is *proposing*. Every proposal is checked against the
tool registry and its argument schema before it becomes a policy question, and
the Policy Engine decides without consulting the model. A model that invents a
tool, misfills an argument or asks for something forbidden produces a rejection
and an audit entry, not an action.

Three guards bound a turn, because an assistant that can call tools can also
loop:

* **calls** — at most ``ai_max_tool_calls_per_turn`` actions, ever, per message;
* **iterations** — at most ``ai_max_iterations`` round trips to the model;
* **time** — the whole turn is wrapped in ``ai_turn_timeout_s``, and the caller
  can cancel it.

None of these are advisory. When one trips, the turn ends and the user is told
why rather than being left waiting.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.ai.provider import (
    AIProvider,
    AIProviderError,
    AIRequest,
    AITimeoutError,
    MalformedResponseError,
    MessageSegment,
    ProposedToolCall,
    Provenance,
    Role,
)
from atlas_backend.ai.redaction import redact_arguments, redact_text
from atlas_backend.audit import AuditActor, AuditEvent, append
from atlas_backend.config import Settings
from atlas_backend.db.models import Device, ToolCall
from atlas_backend.logging import get_logger
from atlas_backend.policy import CallStatus, ToolDispatcher
from atlas_shared.enums import Language
from atlas_shared.tools.catalog import CATALOG

__all__ = ["Assistant", "RejectedProposal", "StopReason", "TurnResult"]

log = get_logger(__name__)

_MAX_USER_TEXT = 4000


class StopReason:
    """Why a turn ended, for the trail and for the user-facing message."""

    COMPLETED = "completed"
    TOOL_CALL_LIMIT = "tool_call_limit"
    ITERATION_LIMIT = "iteration_limit"
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RejectedProposal:
    """A tool call the model proposed that never reached the Policy Engine."""

    tool: str
    reason: str
    detail: str


@dataclass(slots=True)
class TurnResult:
    reply: str
    language: Language
    executed: list[ToolCall] = field(default_factory=list)
    pending: list[ToolCall] = field(default_factory=list)
    denied: list[ToolCall] = field(default_factory=list)
    rejected: list[RejectedProposal] = field(default_factory=list)
    iterations: int = 0
    stopped_because: str = StopReason.COMPLETED
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def acted(self) -> bool:
        return bool(self.executed or self.pending or self.denied)


class Assistant:
    def __init__(
        self,
        *,
        provider: AIProvider,
        dispatcher: ToolDispatcher,
        settings: Settings,
    ) -> None:
        self._provider = provider
        self._dispatcher = dispatcher
        self._settings = settings

    async def handle(
        self,
        session: AsyncSession,
        *,
        target: Device,
        requester: Device,
        text: str,
        language: Language = Language.RU,
        message_id: uuid.UUID | None = None,
    ) -> TurnResult:
        """Run one user message to completion. Never raises for model failures."""
        user_text = text.strip()[:_MAX_USER_TEXT]

        await append(
            session,
            actor=AuditActor.USER,
            event_type=AuditEvent.ASSISTANT_TURN_STARTED,
            device_id=target.id,
            payload={"text": redact_text(user_text), "language": language.value},
        )

        try:
            async with asyncio.timeout(self._settings.ai_turn_timeout_s):
                result = await self._run(
                    session,
                    target=target,
                    requester=requester,
                    user_text=user_text,
                    language=language,
                    message_id=message_id,
                )
        except TimeoutError:
            result = TurnResult(
                reply=_message(language, "timeout"),
                language=language,
                stopped_because=StopReason.TIMEOUT,
            )
        except asyncio.CancelledError:
            # The caller went away. Record it and re-raise: cancellation is not
            # something to swallow.
            await append(
                session,
                actor=AuditActor.USER,
                event_type=AuditEvent.ASSISTANT_TURN_COMPLETED,
                device_id=target.id,
                payload={"stopped_because": StopReason.CANCELLED},
            )
            raise

        await append(
            session,
            actor=AuditActor.USER,
            event_type=AuditEvent.ASSISTANT_TURN_COMPLETED,
            device_id=target.id,
            payload={
                "stopped_because": result.stopped_because,
                "iterations": result.iterations,
                "executed": len(result.executed),
                "pending": len(result.pending),
                "denied": len(result.denied),
                "rejected": len(result.rejected),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )
        return result

    # ------------------------------------------------------------------ loop

    async def _run(
        self,
        session: AsyncSession,
        *,
        target: Device,
        requester: Device,
        user_text: str,
        language: Language,
        message_id: uuid.UUID | None,
    ) -> TurnResult:
        result = TurnResult(reply="", language=language)
        segments: list[MessageSegment] = [
            MessageSegment(role=Role.USER, text=user_text, provenance=Provenance.USER_INSTRUCTION)
        ]
        remaining_calls = self._settings.ai_max_tool_calls_per_turn
        has_external_content = False
        descriptors = CATALOG.descriptors()

        for iteration in range(1, self._settings.ai_max_iterations + 1):
            result.iterations = iteration

            try:
                response = await self._provider.complete(
                    AIRequest(
                        segments=tuple(segments),
                        tools=descriptors,
                        language=language,
                        has_external_content=has_external_content,
                    )
                )
            except (AITimeoutError, MalformedResponseError, AIProviderError) as exc:
                log.warning("ai_provider_failed", error=type(exc).__name__)
                result.reply = _message(language, _provider_failure_key(exc))
                result.stopped_because = StopReason.PROVIDER_UNAVAILABLE
                return result

            result.input_tokens += response.input_tokens or 0
            result.output_tokens += response.output_tokens or 0

            if not response.wants_tools:
                # A plain answer, or a clarifying question. Either way, done.
                result.reply = response.text
                result.stopped_because = StopReason.COMPLETED
                return result

            proposals = list(response.tool_calls)
            if len(proposals) > remaining_calls:
                log.warning(
                    "ai_tool_call_limit",
                    proposed=len(proposals),
                    remaining=remaining_calls,
                )
                proposals = proposals[:remaining_calls]
                result.stopped_because = StopReason.TOOL_CALL_LIMIT

            feedback = await self._perform(
                session,
                proposals=proposals,
                target=target,
                requester=requester,
                result=result,
                external_content_present=has_external_content,
                message_id=message_id,
            )
            remaining_calls -= len(proposals)

            segments.append(
                MessageSegment(
                    role=Role.ASSISTANT,
                    text=response.text or "(calling tools)",
                    provenance=Provenance.USER_INSTRUCTION,
                )
            )
            segments.extend(feedback)
            # Everything a tool returns is attacker-influenceable: a filename is
            # chosen by whoever created the file.
            has_external_content = True

            if remaining_calls <= 0:
                result.stopped_because = StopReason.TOOL_CALL_LIMIT
                break
        else:
            result.stopped_because = StopReason.ITERATION_LIMIT

        # The loop ended without the model getting a last word. Summarise from
        # what actually happened rather than spending another call on it.
        result.reply = _summarise(result, language)
        return result

    async def _perform(
        self,
        session: AsyncSession,
        *,
        proposals: list[ProposedToolCall],
        target: Device,
        requester: Device,
        result: TurnResult,
        external_content_present: bool,
        message_id: uuid.UUID | None,
    ) -> list[MessageSegment]:
        feedback: list[MessageSegment] = []

        for proposal in proposals:
            rejection = self._validate(proposal)
            if rejection is not None:
                result.rejected.append(rejection)
                await append(
                    session,
                    actor=AuditActor.SYSTEM,
                    event_type=AuditEvent.MODEL_PROPOSAL_REJECTED,
                    device_id=target.id,
                    payload={
                        "tool": proposal.tool,
                        "reason": rejection.reason,
                        "detail": rejection.detail,
                        "args": redact_arguments(proposal.args),
                    },
                )
                feedback.append(
                    MessageSegment(
                        role=Role.USER,
                        text=f"REJECTED: {rejection.detail}",
                        provenance=Provenance.TOOL_RESULT,
                        tool_name=proposal.tool,
                    )
                )
                continue

            await append(
                session,
                actor=AuditActor.SYSTEM,
                event_type=AuditEvent.MODEL_PROPOSED_TOOL,
                device_id=target.id,
                payload={
                    "tool": proposal.tool,
                    "args": redact_arguments(proposal.args),
                },
            )

            outcome = await self._dispatcher.execute(
                session,
                target=target,
                requester=requester,
                tool_name=proposal.tool,
                args=proposal.args,
                external_content_present=external_content_present,
                message_id=message_id,
            )
            call = outcome.call

            if call.status == CallStatus.PENDING_CONFIRMATION:
                result.pending.append(call)
            elif call.decision == "deny":
                result.denied.append(call)
            else:
                result.executed.append(call)

            feedback.append(
                MessageSegment(
                    role=Role.USER,
                    text=_describe_outcome(call),
                    provenance=Provenance.TOOL_RESULT,
                    tool_name=call.tool_name,
                )
            )

        return feedback

    def _validate(self, proposal: ProposedToolCall) -> RejectedProposal | None:
        """Check a proposal against the registry before policy ever sees it."""
        if not CATALOG.has(proposal.tool):
            # The model invented a tool, or asked for a shell. Neither exists.
            return RejectedProposal(
                tool=proposal.tool,
                reason="unknown_tool",
                detail=(
                    f"There is no tool called '{proposal.tool}'. Only the tools in "
                    "your tool list exist; there is no shell or command runner."
                ),
            )

        try:
            CATALOG.get(proposal.tool).validate_args(proposal.args)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_url=False, include_input=False)[:4]
            )
            return RejectedProposal(
                tool=proposal.tool,
                reason="invalid_arguments",
                detail=f"Arguments rejected for {proposal.tool}: {problems}",
            )
        except (TypeError, ValueError) as exc:
            # The type says the arguments are a mapping, and Gemini's parser
            # enforces that. A third-party provider might not, and a non-mapping
            # would blow up inside validate_args rather than failing validation.
            return RejectedProposal(
                tool=proposal.tool,
                reason="invalid_arguments",
                detail=f"Arguments for {proposal.tool} are not usable: {type(exc).__name__}",
            )

        return None


# --------------------------------------------------------------------- text


def _describe_outcome(call: ToolCall) -> str:
    """What the model is told about a call, in a form it can report."""
    if call.status == CallStatus.PENDING_CONFIRMATION:
        return (
            f"HELD: {call.tool_name} requires the user's confirmation "
            f"(risk: {call.risk_assessed}). It has NOT run. Tell the user it is "
            "waiting for them to confirm."
        )
    if call.decision == "deny":
        reason = "; ".join(call.policy_reasons) or "policy refused it"
        return f"DENIED: {call.tool_name} was refused. Reason: {reason}. It did not run."
    if call.status == CallStatus.UNREACHABLE:
        return f"FAILED: {call.tool_name} could not run — the computer is not connected."
    if call.status == CallStatus.TIMEOUT:
        return f"FAILED: {call.tool_name} did not finish in time."
    if call.refusal:
        return f"REFUSED by the computer: {call.tool_name} ({call.refusal}). It did not run."
    if call.error:
        return f"ERROR from {call.tool_name}: {call.error.get('message', 'unknown error')}"

    return f"OK: {call.tool_name} returned {redact_arguments(call.result)}"


_MESSAGES: dict[str, dict[Language, str]] = {
    "timeout": {
        Language.RU: (
            "Не успел обработать запрос за отведённое время. "
            "Ничего не выполнено сверх того, что уже подтверждено."
        ),
        Language.EN: "The request took too long. Nothing ran beyond what is already reported.",
        Language.KK: "Сұрау уақытында аяқталмады.",
    },
    "unavailable": {
        Language.RU: "Языковая модель сейчас недоступна. Команды через неё выполнить не могу.",
        Language.EN: "The language model is unavailable right now, so I cannot act on that.",
        Language.KK: "Тілдік модель қазір қолжетімсіз.",
    },
    "malformed": {
        Language.RU: (
            "Модель вернула ответ, который я не смог разобрать. Попробуйте переформулировать."
        ),
        Language.EN: "The model returned something I could not read. Try rephrasing.",
        Language.KK: "Модель жауабын оқи алмадым.",
    },
}


def _message(language: Language, key: str) -> str:
    options = _MESSAGES[key]
    return options.get(language, options[Language.EN])


def _provider_failure_key(error: AIProviderError) -> str:
    return "malformed" if isinstance(error, MalformedResponseError) else "unavailable"


def _summarise(result: TurnResult, language: Language) -> str:
    """A deterministic report, used when the model does not get a last word."""
    russian = language is Language.RU
    parts: list[str] = []

    if result.executed:
        names = ", ".join(call.tool_name for call in result.executed)
        parts.append(f"Выполнено: {names}." if russian else f"Done: {names}.")
    if result.pending:
        names = ", ".join(call.tool_name for call in result.pending)
        parts.append(
            f"Ждёт вашего подтверждения: {names}."
            if russian
            else f"Waiting for your confirmation: {names}."
        )
    if result.denied:
        names = ", ".join(call.tool_name for call in result.denied)
        parts.append(
            f"Отклонено политикой: {names}." if russian else f"Refused by policy: {names}."
        )
    if result.rejected:
        parts.append(
            f"Отброшено некорректных вызовов: {len(result.rejected)}."
            if russian
            else f"Discarded invalid tool calls: {len(result.rejected)}."
        )

    if result.stopped_because == StopReason.TOOL_CALL_LIMIT:
        parts.append(
            "Достигнут предел числа действий на один запрос — остановился."
            if russian
            else "Reached the per-request action limit and stopped."
        )
    elif result.stopped_because == StopReason.ITERATION_LIMIT:
        parts.append(
            "Достигнут предел числа обращений к модели — остановился."
            if russian
            else "Reached the model round-trip limit and stopped."
        )

    if not parts:
        return "Ничего не выполнено." if russian else "Nothing was done."
    return " ".join(parts)
