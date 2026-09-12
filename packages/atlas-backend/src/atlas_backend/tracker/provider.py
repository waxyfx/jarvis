"""What a tracker is, from JARVIS's side of the wire.

The tracker is Sunny, and this file is where that stops being true. Everything
above it talks to :class:`TrackerProvider`; only :mod:`atlas_backend.tracker.sunny`
knows about Next.js, Prisma or a machine token. That is not ceremony — it is what
lets the summariser and the tool handlers be tested without a running web app,
and it is what stops "the tracker" and "Sunny" becoming the same word in forty
places.

The types are deliberately smaller than Sunny's. A task in Sunny has subtasks,
comments, attachments, tags, a project, a goal, an order and a recurrence rule;
a task *spoken aloud* has a title, a time, a priority and whether it is late.
Carrying the rest through would mean the model reads it, and the model reading
it is the problem this integration has to solve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Goal",
    "Habit",
    "Priority",
    "Task",
    "TrackerError",
    "TrackerProvider",
    "TrackerUnavailableError",
]


class TrackerError(RuntimeError):
    """The tracker could not answer. Never contains the token."""


class TrackerUnavailableError(TrackerError):
    """No tracker is configured, or it cannot be reached."""


class Priority(StrEnum):
    """Sunny's four levels, spelled as Sunny spells them.

    Not an approximation: these are the strings its zod schema accepts, and
    inventing a fifth here would mean a translation layer that can only lose.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"

    @property
    def is_pressing(self) -> bool:
        return self in (Priority.HIGH, Priority.URGENT)


@dataclass(frozen=True, slots=True)
class Task:
    """One task, reduced to what can be said out loud."""

    id: str
    title: str
    priority: Priority = Priority.MEDIUM
    #: When it is due, if it is due. Date-only deadlines have no ``at``.
    deadline: datetime | None = None
    #: "15:00", when a time was actually set. Separate from the deadline
    #: because "Thursday" and "Thursday at three" are different answers.
    at: str | None = None
    done: bool = False
    project: str | None = None

    def is_overdue(self, *, now: datetime) -> bool:
        if self.done or self.deadline is None:
            return False
        return self.deadline < now


@dataclass(frozen=True, slots=True)
class Goal:
    id: str
    title: str
    progress: int = 0
    target_date: date | None = None


@dataclass(frozen=True, slots=True)
class Habit:
    id: str
    title: str
    done_today: bool = False
    streak: int = 0


@dataclass(frozen=True, slots=True)
class Applied:
    """The result of a write, in the words the owner should hear back.

    ``what`` names the thing that changed, because "done" is not an answer when
    the question was which task. A misheard title that reaches this far has to
    be visible in the reply.
    """

    what: str
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TrackerProvider(Protocol):
    """Everything JARVIS may ask a tracker to do, and nothing more.

    There is no ``request`` method and no ``call`` method taking a path. The
    absence is the design: the model proposes one of these by name with checked
    arguments, and there is no function anywhere that would turn model output
    into an arbitrary HTTP call.
    """

    name: str

    # ------------------------------------------------------------- reading

    async def tasks_today(self) -> list[Task]: ...

    async def tasks_upcoming(self, *, days: int = 7) -> list[Task]: ...

    async def goals(self) -> list[Goal]: ...

    async def habits(self) -> list[Habit]: ...

    # ------------------------------------------------------------- writing

    async def add_task(
        self,
        *,
        title: str,
        priority: Priority = Priority.MEDIUM,
        deadline: datetime | None = None,
    ) -> Applied: ...

    async def add_goal(self, *, title: str, target_date: date | None = None) -> Applied: ...

    async def complete_task(self, *, task_id: str) -> Applied: ...

    async def reschedule_task(self, *, task_id: str, deadline: datetime) -> Applied: ...

    async def set_priority(self, *, task_id: str, priority: Priority) -> Applied: ...
