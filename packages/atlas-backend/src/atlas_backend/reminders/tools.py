"""Setting, listing and cancelling a reminder.

"Напомни мне через двадцать минут позвонить маме" is not a task. A task belongs
in the tracker, which the owner maintains and looks at; this is a thought they
do not want to carry for twenty minutes, and putting it in the tracker would
fill the tracker with things to tidy up afterwards.

The model gives a delay or a time and a sentence. Everything else — whether it
is said aloud, whether anyone is there to hear it, whether it is quiet hours —
is already decided by the proactive rules, so nothing here needs an opinion.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.db.base import utc_now
from atlas_backend.db.models import Reminder

__all__ = ["REMINDER_TOOLS", "ReminderToolError", "run_reminder_tool"]


class ReminderToolError(RuntimeError):
    """Something to say out loud, rather than something to crash on."""


#: More than this and it belongs in the tracker, where it will still exist
#: after a restart of anything. A reminder is for the next few hours.
_FURTHEST_AHEAD = timedelta(days=7)

#: How many are read back. Someone with forty pending reminders has a different
#: problem, and reading forty aloud is not the answer to it.
_NAMED = 10


def _when(args: Mapping[str, Any], *, now: datetime) -> datetime:
    """The moment asked for, from either shape the model may use."""
    minutes = args.get("in_minutes")
    if minutes is not None:
        return now + timedelta(minutes=int(minutes))

    at = args.get("at")
    if not at:
        raise ReminderToolError("say when: either in_minutes or at")

    try:
        parsed = datetime.fromisoformat(str(at).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        # Named, so the assistant can ask again rather than reporting that
        # something is broken.
        raise ReminderToolError(
            f"at is not a time I can use: {at!r}. Give it as ISO 8601."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _set(
    session: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID, args: Mapping[str, Any]
) -> dict[str, Any]:
    now = utc_now()
    due = _when(args, now=now)

    if due <= now:
        raise ReminderToolError("that time has already passed")
    if due - now > _FURTHEST_AHEAD:
        raise ReminderToolError("that is more than a week away — put it in the tracker instead")

    reminder = Reminder(
        user_id=user_id,
        device_id=device_id,
        text=str(args["text"]).strip()[:300],
        due_at=due,
    )
    session.add(reminder)
    await session.flush()

    minutes = round((due - now).total_seconds() / 60)
    return {"set": reminder.text, "in_minutes": minutes, "id": str(reminder.id)}


async def _list(
    session: AsyncSession, user_id: uuid.UUID, _device_id: uuid.UUID, _: Mapping[str, Any]
) -> dict[str, Any]:
    rows = await session.execute(
        select(Reminder)
        .where(
            Reminder.user_id == user_id,
            Reminder.delivered_at.is_(None),
            Reminder.cancelled_at.is_(None),
        )
        .order_by(Reminder.due_at)
        .limit(_NAMED)
    )
    pending = list(rows.scalars())

    return {
        "total": len(pending),
        "reminders": [
            {
                "id": str(item.id),
                "text": item.text,
                "in_minutes": max(0, round((item.due_at - utc_now()).total_seconds() / 60)),
            }
            for item in pending
        ],
    }


async def _cancel(
    session: AsyncSession, user_id: uuid.UUID, _device_id: uuid.UUID, args: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        identifier = uuid.UUID(str(args["reminder_id"]))
    except ValueError as exc:
        raise ReminderToolError("that is not a reminder id") from exc

    found = await session.execute(
        select(Reminder).where(Reminder.id == identifier, Reminder.user_id == user_id)
    )
    reminder = found.scalar_one_or_none()
    if reminder is None or reminder.cancelled_at is not None:
        # The same answer either way. "There is no such reminder" and "you
        # already cancelled it" lead to the same next action.
        raise ReminderToolError("there is no reminder waiting with that id")

    reminder.cancelled_at = utc_now()
    await session.flush()
    # The text, not "ok": a misheard id that got this far has to reach the
    # owner's ears.
    return {"cancelled": reminder.text}


Handler = Callable[
    [AsyncSession, uuid.UUID, uuid.UUID, Mapping[str, Any]], Awaitable[dict[str, Any]]
]

#: The whole surface. A tool not in here cannot be run, whatever the catalogue
#: says and whatever the model asks for.
REMINDER_TOOLS: dict[str, Handler] = {
    "reminder.set": _set,
    "reminder.list": _list,
    "reminder.cancel": _cancel,
}


async def run_reminder_tool(
    session: AsyncSession,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    name: str,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    handler = REMINDER_TOOLS.get(name)
    if handler is None:
        raise ReminderToolError(f"{name} is not something I can do with reminders")
    return await handler(session, user_id, device_id, args)
