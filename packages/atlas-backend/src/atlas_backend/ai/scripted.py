"""A provider that answers from a script.

Shipped rather than confined to the test tree, for two reasons. It is the only
way to make the adversarial cases deterministic — you cannot ask a real model to
reliably invent a tool that does not exist — and it lets the whole pipeline be
exercised on a machine with no API key at all.

It implements :class:`~atlas_backend.ai.provider.AIProvider` exactly, so anything
that works against it works against Gemini.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from atlas_backend.ai.provider import (
    AIProviderError,
    AIRequest,
    AIResponse,
    FinishReason,
    ProposedToolCall,
)

__all__ = ["ScriptedProvider", "text_reply", "tool_reply"]


def text_reply(text: str, *, tokens: tuple[int, int] = (10, 5)) -> AIResponse:
    return AIResponse(
        finish_reason=FinishReason.CLARIFICATION if text.endswith("?") else FinishReason.TEXT,
        text=text,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        model="scripted-1",
    )


def tool_reply(
    *calls: tuple[str, dict[str, Any]], text: str = "", tokens: tuple[int, int] = (20, 15)
) -> AIResponse:
    return AIResponse(
        finish_reason=FinishReason.TOOL_CALLS,
        text=text,
        tool_calls=tuple(
            ProposedToolCall(tool=name, args=args, call_ref=f"c{index}")
            for index, (name, args) in enumerate(calls)
        ),
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        model="scripted-1",
    )


class ScriptedProvider:
    """Returns the next scripted response, or raises the next scripted error."""

    name = "scripted"

    def __init__(
        self,
        script: Sequence[AIResponse | BaseException],
        *,
        model: str = "scripted-1",
    ) -> None:
        self._script = list(script)
        self.model = model
        #: Every request this provider was given, for assertions about what the
        #: model was actually shown — tools, provenance framing, language.
        self.requests: list[AIRequest] = []

    async def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)

        if not self._script:
            # Running past the end of the script is a test bug, and saying so is
            # more useful than a confusing downstream failure.
            raise AIProviderError("scripted provider exhausted")

        nxt = self._script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    @property
    def calls_made(self) -> int:
        return len(self.requests)

    @property
    def exhausted(self) -> bool:
        return not self._script
