"""Remembering, listing and forgetting — the three the owner can ask for.

There is deliberately no fourth. Nothing here lets the model decide on its own
that something is worth remembering: every row exists because a person said
"запомни". An assistant that quietly builds a profile is one whose behaviour
changes for reasons its owner cannot name, and that is worse than one that
forgets.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.memory.store import MOST_REMEMBERED, forget, over_the_limit, recall, remember

__all__ = ["MEMORY_TOOLS", "MemoryToolError", "run_memory_tool"]


class MemoryToolError(RuntimeError):
    """Something to say out loud, rather than something to crash on."""


async def _remember(
    session: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID, args: Mapping[str, Any]
) -> dict[str, Any]:
    text = str(args["text"]).strip()
    if not text:
        raise MemoryToolError("there is nothing to remember")

    existing = await recall(session, user_id=user_id)
    if any(item.text.casefold() == text.casefold() for item in existing):
        # Not an error. Saying it twice is a thing people do, and the honest
        # answer is that it was already known rather than a second identical row.
        return {"already_known": text}

    stored = await remember(session, user_id=user_id, text=text, device_id=device_id)
    result: dict[str, Any] = {"remembered": stored.text, "id": str(stored.id)}
    if len(existing) + 1 > MOST_REMEMBERED:
        # Said out loud rather than silently dropped: the one failure this must
        # not have is appearing to remember something it will never use.
        result["note"] = (
            f"more than {MOST_REMEMBERED} facts are stored; the oldest are no longer "
            "carried into conversations"
        )
    return result


async def _list(
    session: AsyncSession, user_id: uuid.UUID, _device_id: uuid.UUID, _: Mapping[str, Any]
) -> dict[str, Any]:
    memories = await recall(session, user_id=user_id)
    result: dict[str, Any] = {
        "total": len(memories),
        "memories": [{"id": str(item.id), "text": item.text} for item in memories],
    }
    dropped = over_the_limit(memories)
    if dropped:
        result["not_carried"] = dropped
    return result


async def _forget(
    session: AsyncSession, user_id: uuid.UUID, _device_id: uuid.UUID, args: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        identifier = uuid.UUID(str(args["memory_id"]))
    except ValueError as exc:
        raise MemoryToolError("that is not a memory id") from exc

    gone = await forget(session, user_id=user_id, memory_id=identifier)
    if gone is None:
        raise MemoryToolError("there is nothing remembered with that id")
    # The text, not "ok". A misheard id that got this far has to reach the
    # owner's ears while they can still say "no, not that one".
    return {"forgot": gone.text}


Handler = Callable[
    [AsyncSession, uuid.UUID, uuid.UUID, Mapping[str, Any]], Awaitable[dict[str, Any]]
]

MEMORY_TOOLS: dict[str, Handler] = {
    "memory.remember": _remember,
    "memory.list": _list,
    "memory.forget": _forget,
}


async def run_memory_tool(
    session: AsyncSession,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    name: str,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    handler = MEMORY_TOOLS.get(name)
    if handler is None:
        raise MemoryToolError(f"{name} is not something I can do with memory")
    return await handler(session, user_id, device_id, args)
