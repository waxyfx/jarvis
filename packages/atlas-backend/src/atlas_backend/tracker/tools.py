"""Running a tracker tool, once policy has allowed it.

The bridge between a validated tool call and the provider. It is deliberately
boring: a name maps to one function, the function calls one provider method,
and the result is shaped for the model. There is no dispatch on strings the
model supplied, no path construction, no place for an instruction to arrive.

The arguments reaching here have already been through the tool manifest, so the
types are what they claim to be. Dates are the exception: they arrive as text
because that is what a model produces, and they are parsed here where a bad one
can be refused with something a person can act on.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from atlas_backend.tracker.digest import summarise
from atlas_backend.tracker.provider import Priority, TrackerError, TrackerProvider

__all__ = ["TRACKER_TOOLS", "run_tracker_tool"]

Handler = Callable[[TrackerProvider, Mapping[str, Any]], Awaitable[dict[str, Any]]]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: Any, *, field: str) -> datetime:
    """A date the model wrote, or a refusal naming what was wrong with it.

    Models produce dates as prose surprisingly often. Failing here with the
    field named means the assistant can ask again; failing inside the HTTP call
    means the owner hears that the tracker is broken.
    """
    if not isinstance(value, str) or not value.strip():
        raise TrackerError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrackerError(
            f"{field} is not a date I can use: {value!r}. Give it as ISO 8601."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_date(value: Any, *, field: str) -> date | None:
    if value is None:
        return None
    return _parse_datetime(value, field=field).date()


def _priority(value: Any) -> Priority:
    # The manifest's pattern has already rejected anything else; this is the
    # belt to that pair of braces, and costs nothing.
    try:
        return Priority(str(value))
    except ValueError:
        return Priority.MEDIUM


def _offset(args: Mapping[str, Any]) -> int | None:
    value = args.get("offset")
    return int(value) if isinstance(value, int) else None


# ------------------------------------------------------------------ reading


async def _today(tracker: TrackerProvider, args: Mapping[str, Any]) -> dict[str, Any]:
    tasks = await tracker.tasks_today()
    return summarise(tasks, now=_now(), offset=_offset(args)).as_result()


async def _upcoming(tracker: TrackerProvider, args: Mapping[str, Any]) -> dict[str, Any]:
    days = int(args.get("days", 7))
    tasks = await tracker.tasks_upcoming(days=days)
    result = summarise(tasks, now=_now(), offset=_offset(args)).as_result()
    result["days"] = days
    return result


async def _schedule(tracker: TrackerProvider, args: Mapping[str, Any]) -> dict[str, Any]:
    """Only the things that happen at a time, in the order they happen.

    A schedule is not a list of tasks. Something due "today" with no hour is a
    thing to do, not an appointment, and including it would make the answer
    longer while making it less like a schedule.
    """
    tasks = [task for task in await tracker.tasks_today() if task.at and not task.done]
    tasks.sort(key=lambda task: task.at or "")
    return summarise(tasks, now=_now(), offset=_offset(args)).as_result()


async def _goals(tracker: TrackerProvider, _: Mapping[str, Any]) -> dict[str, Any]:
    goals = await tracker.goals()
    return {
        "total": len(goals),
        # Goals are few and slow-moving, so naming them is the right answer
        # where naming eleven tasks is not.
        "goals": [
            {"title": goal.title, "progress": goal.progress} for goal in goals[:_GOALS_NAMED]
        ],
        **({"more": len(goals) - _GOALS_NAMED} if len(goals) > _GOALS_NAMED else {}),
    }


async def _habits(tracker: TrackerProvider, _: Mapping[str, Any]) -> dict[str, Any]:
    habits = await tracker.habits()
    done = [habit for habit in habits if habit.done_today]
    return {
        "total": len(habits),
        "done_today": len(done),
        # What is left is the actionable half; what is finished needs no names.
        "outstanding": [habit.title for habit in habits if not habit.done_today][:_HABITS_NAMED],
        "best_streak": max((habit.streak for habit in habits), default=0),
    }


#: Goals and habits are counted in single figures, so they are named rather
#: than summarised. Tasks are not, which is why they have a digest.
_GOALS_NAMED = 5
_HABITS_NAMED = 5


# ------------------------------------------------------------------ writing


async def _add_task(tracker: TrackerProvider, args: Mapping[str, Any]) -> dict[str, Any]:
    deadline = args.get("deadline")
    applied = await tracker.add_task(
        title=str(args["title"]).strip(),
        priority=_priority(args.get("priority", "medium")),
        deadline=_parse_datetime(deadline, field="deadline") if deadline else None,
    )
    return {"added": applied.what}


async def _add_goal(tracker: TrackerProvider, args: Mapping[str, Any]) -> dict[str, Any]:
    applied = await tracker.add_goal(
        title=str(args["title"]).strip(),
        target_date=_parse_date(args.get("target_date"), field="target_date"),
    )
    return {"added": applied.what}


async def _complete_task(tracker: TrackerProvider, args: Mapping[str, Any]) -> dict[str, Any]:
    applied = await tracker.complete_task(task_id=str(args["task_id"]))
    # The title, not "ok": the owner has to hear *which* task, because a
    # misheard one is exactly what this is guarding against.
    return {"completed": applied.what}


async def _reschedule_task(tracker: TrackerProvider, args: Mapping[str, Any]) -> dict[str, Any]:
    applied = await tracker.reschedule_task(
        task_id=str(args["task_id"]),
        deadline=_parse_datetime(args.get("deadline"), field="deadline"),
    )
    return {"moved": applied.what, "to": applied.detail}


async def _set_priority(tracker: TrackerProvider, args: Mapping[str, Any]) -> dict[str, Any]:
    applied = await tracker.set_priority(
        task_id=str(args["task_id"]), priority=_priority(args["priority"])
    )
    return {"changed": applied.what, "priority": str(args["priority"])}


#: The whole surface. A tool not in here cannot be run, whatever the catalogue
#: says and whatever the model asks for.
TRACKER_TOOLS: dict[str, Handler] = {
    "tracker.today": _today,
    "tracker.upcoming": _upcoming,
    "tracker.schedule": _schedule,
    "tracker.goals": _goals,
    "tracker.habits": _habits,
    "tracker.add_task": _add_task,
    "tracker.add_goal": _add_goal,
    "tracker.complete_task": _complete_task,
    "tracker.reschedule_task": _reschedule_task,
    "tracker.set_priority": _set_priority,
}


async def run_tracker_tool(
    tracker: TrackerProvider, name: str, args: Mapping[str, Any]
) -> dict[str, Any]:
    """Run one tracker tool by name.

    Raises :class:`TrackerError` for anything the caller should report rather
    than crash on — an unknown name included, since a catalogue entry without a
    handler is a programming mistake that should surface as a refusal rather
    than a stack trace in front of someone.
    """
    handler = TRACKER_TOOLS.get(name)
    if handler is None:
        raise TrackerError(f"{name} is not something the tracker can do")
    return await handler(tracker, args)
