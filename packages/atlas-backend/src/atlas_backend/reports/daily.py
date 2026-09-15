"""The day, written down where the owner already keeps things.

A report nobody reads is worse than no report, so this does not invent a place
to put one. It writes a note in Sunny, alongside everything else the owner
records about their own life, tagged so it can be found and filtered out.

**It is written, not spoken.** The evening summary is one sentence said aloud;
this is the detail behind it — which tasks, which applications, how long. Two
different things, deliberately: the spoken one is for someone about to stop for
the day, the written one is for someone looking back at the week.

**It is written whether or not anyone is at the machine.** Unlike a
notification, nothing here depends on being heard. The day happened; the record
of it should exist by morning.

**It says what it does not know.** A day with the agent switched off produces a
report that says the computer was not being watched, rather than a report
claiming zero hours — those are different facts and only one of them is true.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from atlas_backend.activity.summary import ActivityDigest
from atlas_backend.tracker.provider import Habit, Task

__all__ = ["DayReport", "NoteSink", "build_report"]

#: Applications named in the report. More than the spoken summary allows, fewer
#: than a log: this is meant to be glanced at, not audited.
_APPS_NAMED = 5

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


class NoteSink(Protocol):
    """Somewhere to put a written report.

    Narrower than :class:`~atlas_backend.tracker.provider.TrackerProvider` on
    purpose. The report needs one capability, and asking for the whole tracker
    would mean anything that can store a note has to pretend to manage tasks.
    """

    async def add_note(self, *, title: str, body: str, tags: Sequence[str] = ()) -> str: ...


def _hours_and_minutes(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def _russian_date(moment: datetime) -> str:
    return f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}"


@dataclass(frozen=True, slots=True)
class DayReport:
    """One day, as it will be filed."""

    on: datetime
    done: Sequence[Task] = ()
    left: Sequence[Task] = ()
    habits: Sequence[Habit] = ()
    activity: ActivityDigest = field(default_factory=ActivityDigest)
    #: Reminders set today that have not gone off yet. Worth a line: a reminder
    #: the owner set this morning and has not heard is the one thing in this
    #: report they might still act on tonight.
    reminders_waiting: Sequence[str] = ()
    #: False when no samples exist for the day at all: the agent was off, or
    #: the machine was. Reported as unknown rather than as zero.
    watched: bool = True

    @property
    def title(self) -> str:
        return f"Итоги дня — {_russian_date(self.on)}"

    @property
    def tags(self) -> tuple[str, ...]:
        # Two tags rather than one: "jarvis" is who wrote it, "итоги-дня" is
        # what it is. Either alone makes the other hard to filter for.
        return ("jarvis", "итоги-дня")

    def as_body(self) -> str:
        return "\n".join(self._sections())

    def _sections(self) -> list[str]:
        sections = [self._tasks()]
        if self.habits:
            sections.append(self._habits())
        sections.append(self._computer())
        if self.reminders_waiting:
            sections.append(self._reminders())
        return [section for section in sections if section]

    def _reminders(self) -> str:
        lines = [f"Напоминания, которые ещё не прозвучали: {len(self.reminders_waiting)}."]
        lines += [f"  — {text}" for text in self.reminders_waiting[:_APPS_NAMED]]
        return "\n".join(lines)

    def _tasks(self) -> str:
        total = len(self.done) + len(self.left)
        if not total:
            return "Задачи: на сегодня ничего не было запланировано."

        lines = [f"Задачи: выполнено {len(self.done)} из {total}."]
        lines += [f"  ✓ {task.title}" for task in self.done]
        # Named rather than counted: what is left is the half that can still be
        # acted on tomorrow, and a number does not say which.
        lines += [f"  — {task.title}" for task in self.left]
        return "\n".join(lines)

    def _habits(self) -> str:
        kept = [habit for habit in self.habits if habit.done_today]
        missed = [habit.title for habit in self.habits if not habit.done_today]
        line = f"Привычки: {len(kept)} из {len(self.habits)}."
        return f"{line}\n  Пропущено: {', '.join(missed)}." if missed else line

    def _computer(self) -> str:
        if not self.watched:
            # The honest answer. "0 минут" would be a claim about the day; this
            # is a claim about the record, which is the only one that holds.
            return "За компьютером: нет данных — агент сегодня не работал."

        lines = [
            f"За компьютером: {_hours_and_minutes(self.activity.active_minutes)} активно, "
            f"{_hours_and_minutes(round(self.activity.idle_s / 60))} простоя."
        ]
        lines += [
            f"  {name} — {_hours_and_minutes(round(seconds / 60))}"
            for name, seconds in self.activity.by_app[:_APPS_NAMED]
            if seconds >= 60
        ]
        longest = round(self.activity.longest_stretch_s / 60)
        if longest:
            lines.append(f"Самый долгий заход без перерыва: {_hours_and_minutes(longest)}.")
        return "\n".join(lines)


def build_report(
    *,
    on: datetime,
    tasks: Sequence[Task],
    habits: Sequence[Habit],
    activity: ActivityDigest,
    watched: bool,
    reminders_waiting: Sequence[str] = (),
) -> DayReport:
    """Assemble the day. Pure: everything it needs is already gathered."""
    return DayReport(
        on=on,
        done=[task for task in tasks if task.done],
        left=[task for task in tasks if not task.done],
        habits=habits,
        activity=activity,
        watched=watched,
        reminders_waiting=reminders_waiting,
    )
