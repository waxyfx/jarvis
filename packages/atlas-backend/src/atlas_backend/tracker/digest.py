"""Turning a list into something worth hearing.

Eleven tasks read aloud is not an answer, it is a punishment. What the owner
wants from a spoken tracker is the shape of the day — how much, how urgent,
what is next — and then the option to hear more.

**The compaction happens here, in code, and that is the whole point.** Telling
the model to "be brief" is a preference it may honour; handing it four facts and
three titles is a guarantee, because it cannot recite a list it was never shown.
The orchestrator feeds the entire tool result back to the model as
``OK: <tool> returned <result>``, so whatever this function returns is exactly
what the model has read. There is one field and it has one audience.

Hearing the rest is therefore a second question, not a bigger answer: ask again
with an offset. That keeps the model's view small at every point in the turn,
which is a stronger guarantee than trimming its output afterwards.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atlas_backend.tracker.provider import Priority, Task

__all__ = ["NAMED_INDIVIDUALLY", "Digest", "summarise"]

#: Up to this many tasks are named one by one; beyond it, counted and
#: characterised. Three is about what someone can hold from a spoken list
#: without asking for it again.
NAMED_INDIVIDUALLY = 3

#: How many a follow-up returns. The same number, for the same reason.
PER_FOLLOW_UP = 3


@dataclass(frozen=True)
class Digest:
    """What the model is given about a set of tasks."""

    total: int
    #: Counts by priority, omitting the empty ones — a zero tells nobody
    #: anything and costs a clause when spoken.
    by_priority: dict[str, int] = field(default_factory=dict)
    overdue: int = 0
    #: The soonest thing with a time, which is what "what's next" means.
    next_up: dict[str, str] | None = None
    #: Named individually. Empty when there is nothing to name.
    items: list[dict[str, Any]] = field(default_factory=list)
    #: True when more exist than were named, so the model knows it may offer.
    more: int = 0
    offset: int = 0

    def as_result(self) -> dict[str, Any]:
        """The tool result. Deliberately small: the model reads all of it."""
        result: dict[str, Any] = {"total": self.total}
        if self.by_priority:
            result["by_priority"] = self.by_priority
        if self.overdue:
            result["overdue"] = self.overdue
        if self.next_up:
            result["next"] = self.next_up
        if self.items:
            result["items"] = self.items
        if self.more:
            result["more"] = self.more
            result["hint"] = (
                f"{self.more} not named here. Offer to read them; if the user "
                f"agrees, call the same tool with offset={self.offset + len(self.items)}."
            )
        return result


def _describe(task: Task) -> dict[str, Any]:
    """One task, in the fields that survive being spoken."""
    described: dict[str, Any] = {"title": task.title, "priority": task.priority.value}
    if task.at:
        described["at"] = task.at
    if task.project:
        described["project"] = task.project
    return described


def summarise(tasks: list[Task], *, now: datetime, offset: int | None = None) -> Digest:
    """Reduce a list of tasks to what is worth saying.

    Two modes, and ``offset`` chooses between them rather than merely shifting a
    window. ``None`` asks "what does the day look like" and gets counts. An
    integer asks "name them, starting here" and gets titles.

    They were one mode once, keyed on whether the offset was zero, and it could
    not work: the first answer's own hint pointed at offset zero, which took the
    caller back to the summary it had just been given. There was no path from
    "eleven tasks" to hearing any of their names. The distinction has to be
    between two questions, not between zero and not-zero.
    """
    if offset is None:
        return _overview(tasks, now=now)
    return _listing(tasks, now=now, offset=max(0, offset))


def _overview(tasks: list[Task], *, now: datetime) -> Digest:
    """What the day looks like. Titles only when there are few enough to hold."""
    if not tasks:
        return Digest(total=0)

    overdue = sum(1 for task in tasks if task.is_overdue(now=now))

    # Counting three things is worse than saying them, and the counts would only
    # restate what was just said one by one.
    if len(tasks) <= NAMED_INDIVIDUALLY:
        return Digest(
            total=len(tasks),
            overdue=overdue,
            items=[_describe(task) for task in tasks],
        )

    counted = Counter(task.priority.value for task in tasks)
    return Digest(
        total=len(tasks),
        by_priority={
            level.value: counted[level.value] for level in Priority if counted[level.value]
        },
        overdue=overdue,
        next_up=_next_up(tasks),
        more=len(tasks),
        offset=0,
    )


def _listing(tasks: list[Task], *, now: datetime, offset: int) -> Digest:
    """Name them, a few at a time, starting where the last answer stopped."""
    window = tasks[offset : offset + PER_FOLLOW_UP]
    return Digest(
        total=len(tasks),
        overdue=sum(1 for task in window if task.is_overdue(now=now)),
        items=[_describe(task) for task in window],
        more=max(0, len(tasks) - offset - len(window)),
        offset=offset,
    )


def _next_up(tasks: list[Task]) -> dict[str, str] | None:
    """The soonest task that has a time, if any has one.

    A deadline without a time is a day, not a moment, and "next" is a question
    about moments. Something due today at three beats something due today.
    """
    timed = [task for task in tasks if task.at and task.deadline and not task.done]
    if not timed:
        return None
    soonest = min(timed, key=lambda task: task.deadline)  # type: ignore[arg-type,return-value]
    return {"title": soonest.title, "at": soonest.at or ""}
