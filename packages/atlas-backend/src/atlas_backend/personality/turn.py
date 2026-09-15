"""Copy a completed turn into the presentation contract; never mutate the turn."""

from __future__ import annotations

from typing import TYPE_CHECKING

from atlas_backend.personality.engine import ReplySnapshot

if TYPE_CHECKING:
    from atlas_backend.ai.orchestrator import TurnResult


def snapshot_turn(result: TurnResult) -> ReplySnapshot:
    """Run after the normal policy/execution/audit path has finished.

    ``executed`` means "attempted" in the orchestrator. Moreover, ToolCall
    stores the dispatch lifecycle status ("completed"), not ToolResult.status.
    A non-OK result can legally contain data without a failure/refusal field.
    Until that execution status reaches the API, every tool-bearing turn stays
    verbatim. This avoids mistaking an attempted operation for a success.
    Only immutable values cross into the provider; SQLAlchemy objects do not.
    """
    protected = result.stopped_because != "completed" or bool(
        result.executed or result.pending or result.denied or result.rejected
    )
    return ReplySnapshot(text=result.reply, language=result.language, protected=protected)
