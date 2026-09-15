"""Deciding whether there is anything worth saying.

Each rule looks at one :class:`Moment` — the clock, the tracker, and what the
owner has been doing — and returns either a sentence or nothing. Nothing is the
usual answer, and the rules are written so that it stays the usual answer.

**The wording is a template, not a model call.** Three reasons, in order: a
notification must be predictable, because the owner cannot argue with something
that was said while they were out of the room; the free tier is twenty requests
a day and a scheduler would spend all of them; and a model asked to phrase a
reminder will eventually phrase one wrongly at four in the afternoon with no
one watching. Personality belongs in the replies to what the owner actually
said.

**Quiet hours are a downgrade, not a filter.** Between the configured hours a
notification is still delivered and still shown — it just is not spoken. The
distinction matters for a reminder about something at eight in the morning:
suppressing it entirely loses information, while saying it aloud at midnight is
the behaviour that gets an assistant switched off.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from atlas_backend.activity.summary import ActivityDigest
from atlas_backend.prayer.times import PrayerTimes
from atlas_backend.tracker.provider import Task
from atlas_shared.enums import NotificationKind, NotificationPriority
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.messages import Notify

__all__ = ["Moment", "Planned", "Schedule", "decide_all"]


@dataclass(frozen=True, slots=True)
class Schedule:
    """When the assistant is allowed to speak, and about what."""

    #: The morning briefing is said once, in this window, on a day the owner is
    #: at the machine. A window rather than "after this hour", which the first
    #: version had: someone who sits down at four in the afternoon was greeted
    #: with a morning briefing about a day that was mostly over. Missing the
    #: window means no briefing, which is the right answer — at four o'clock,
    #: "here is what today holds" is not news.
    briefing_hour: int = 8
    briefing_until_hour: int = 12
    #: The same shape for the evening. The upper bound keeps it out of the small
    #: hours for someone still working at two in the morning, who does not need
    #: yesterday summarised at them.
    evening_hour: int = 21
    evening_until_hour: int = 23
    #: How close a timed task has to be before it is worth mentioning. Fifteen
    #: minutes is enough to finish a paragraph and walk somewhere.
    remind_before_minutes: int = 15
    #: Unbroken time at the desk before the assistant says something about it.
    long_session_minutes: int = 90
    #: And how long before it says it again, for someone who did not stop.
    long_session_repeat_minutes: int = 60
    #: How long before each prayer to say something. Zero switches the
    #: reminders off while leaving the question answerable by asking.
    prayer_reminder_minutes: int = 10
    #: Spoken notifications are held back outside these hours — shown, not said.
    quiet_from_hour: int = 23
    quiet_until_hour: int = 7


@dataclass(frozen=True, slots=True)
class Moment:
    """Everything the rules are allowed to look at, gathered once.

    A snapshot rather than a set of accessors so that every rule sees the same
    instant. Rules that each fetch their own "now" disagree about which day it
    is at midnight, which is the one night nobody is awake to notice.
    """

    now: datetime
    #: Today's tasks, in the owner's tracker. Empty when there is no tracker
    #: configured, which the rules treat as "nothing to say" rather than as an
    #: error.
    tasks: Sequence[Task] = ()
    #: Reminders that are due now and have not been said. Already filtered
    #: by the scheduler, because the query is a database question.
    due_reminders: Sequence[tuple[str, str]] = ()
    #: Today's prayer times, when the owner has configured a location.
    #: Absent is the normal state and means the rule has nothing to say.
    prayers: PrayerTimes | None = None
    activity: ActivityDigest = field(default_factory=ActivityDigest)
    #: Whether the owner is actually at the machine right now. Nothing is said
    #: to an empty chair.
    present: bool = False


@dataclass(frozen=True, slots=True)
class Planned:
    """A notification, and the key that stops it being sent twice."""

    notification: Notify
    #: Unique per thing-worth-saying-once: a task on a date, a briefing on a
    #: date, a wellness nudge in a given hour.
    key: str


def _is_quiet(now: datetime, schedule: Schedule) -> bool:
    hour = now.hour
    if schedule.quiet_from_hour <= schedule.quiet_until_hour:
        return schedule.quiet_from_hour <= hour < schedule.quiet_until_hour
    # The normal case: the window wraps past midnight.
    return hour >= schedule.quiet_from_hour or hour < schedule.quiet_until_hour


def _notify(
    kind: NotificationKind,
    title: str,
    body: str,
    *,
    now: datetime,
    schedule: Schedule,
    priority: NotificationPriority = NotificationPriority.NORMAL,
) -> Notify:
    # One rule, not two: LOW means shown and not spoken, and quiet hours work by
    # downgrading to LOW. Deriving `speak` from the priority rather than setting
    # both independently is what stops a notification claiming to be low
    # priority and asking to be read aloud in the same breath.
    effective = NotificationPriority.LOW if _is_quiet(now, schedule) else priority
    return Notify(
        notification_id=new_ulid(),
        kind=kind,
        priority=effective,
        title=title,
        body=body,
        speak=effective is not NotificationPriority.LOW,
    )


# ----------------------------------------------------------------- wording
#
# Russian, because that is what the owner speaks to it in, and written to be
# heard rather than read: no lists, no numbers in digits where a word will do,
# and short enough that the end of the sentence is still in mind at the start.


def _hours_and_minutes(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def _task_phrase(task: Task) -> str:
    return f"{task.title} в {task.at}" if task.at else task.title


# ------------------------------------------------------------------- rules


def _due_soon(moment: Moment, schedule: Schedule) -> list[Planned]:
    """Something timed is about to start.

    Only things with an hour on them. A task due today with no time is a thing
    to do, not an appointment, and announcing it at an arbitrary minute is an
    interruption the owner did not schedule.
    """
    if not moment.present:
        return []

    window = timedelta(minutes=schedule.remind_before_minutes)
    planned: list[Planned] = []

    for task in moment.tasks:
        if task.done or not task.at:
            continue
        starts = _at_today(moment.now, task.at)
        if starts is None or not (moment.now <= starts <= moment.now + window):
            continue

        minutes = max(1, round((starts - moment.now).total_seconds() / 60))
        planned.append(
            Planned(
                key=f"reminder:{task.id}:{moment.now.date().isoformat()}",
                notification=_notify(
                    NotificationKind.REMINDER,
                    task.title,
                    f"Через {minutes} мин — {_task_phrase(task)}.",
                    now=moment.now,
                    schedule=schedule,
                ),
            )
        )

    return planned


def _at_today(now: datetime, at: str) -> datetime | None:
    """ "15:00" as a moment on the day ``now`` falls in, or None if unparseable.

    The tracker's hour is a string because that is how it is stored and how it
    is spoken. A malformed one is data, not a crash: the rule simply has nothing
    to say about that task.
    """
    try:
        hour, _, minute = at.partition(":")
        return datetime.combine(now.date(), time(int(hour), int(minute or 0)), tzinfo=now.tzinfo)
    except (ValueError, TypeError):
        return None


def _morning_briefing(moment: Moment, schedule: Schedule) -> list[Planned]:
    """What the day holds, once, when the owner turns up."""
    if not moment.present:
        return []
    if not schedule.briefing_hour <= moment.now.hour < schedule.briefing_until_hour:
        return []
    if _is_quiet(moment.now, schedule):
        # A briefing nobody hears is not worth spending the once-a-day on: it
        # would be marked said, and the real morning would pass in silence.
        return []

    timed = [task for task in moment.tasks if task.at and not task.done]
    outstanding = [task for task in moment.tasks if not task.done]

    if not outstanding:
        body = "На сегодня в трекере ничего не запланировано, сэр."
    elif timed:
        body = (
            f"Сегодня у вас {_count(len(outstanding))}. "
            f"Первое по времени — {_task_phrase(timed[0])}."
        )
    else:
        body = f"Сегодня у вас {_count(len(outstanding))}, без привязки ко времени."

    # One more sentence, and only when there is a prayer still ahead. The
    # briefing is the one thing said aloud in the morning, so anything that
    # belongs in a morning belongs here rather than in a second interruption.
    upcoming = moment.prayers.next_after(moment.now) if moment.prayers else None
    if upcoming is not None:
        prayer, at = upcoming
        body += f" {prayer.russian} в {at.strftime('%H:%M')}."

    return [
        Planned(
            key=f"briefing:{moment.now.date().isoformat()}",
            notification=_notify(
                NotificationKind.BRIEFING,
                "Утренняя сводка",
                body,
                now=moment.now,
                schedule=schedule,
            ),
        )
    ]


def _evening_summary(moment: Moment, schedule: Schedule) -> list[Planned]:
    """What the day held. Said whether or not it went well."""
    if not moment.present:
        return []
    if not schedule.evening_hour <= moment.now.hour < schedule.evening_until_hour:
        return []
    if _is_quiet(moment.now, schedule):
        return []

    done = [task for task in moment.tasks if task.done]
    left = [task for task in moment.tasks if not task.done]
    worked = _hours_and_minutes(moment.activity.active_minutes)

    parts = [f"За компьютером сегодня {worked}."]
    if moment.tasks:
        parts.append(f"Выполнено {len(done)} из {len(moment.tasks)}.")
    if len(left) == 1:
        # Naming the one thing left is useful. Naming six is a to-do list read
        # aloud at nine in the evening, which helps nobody sleep.
        parts.append(f"Осталось: {_task_phrase(left[0])}.")

    return [
        Planned(
            key=f"summary:{moment.now.date().isoformat()}",
            notification=_notify(
                NotificationKind.SUMMARY,
                "Итоги дня",
                " ".join(part for part in parts if part),
                now=moment.now,
                schedule=schedule,
                priority=NotificationPriority.LOW,
            ),
        )
    ]


def _long_session(moment: Moment, schedule: Schedule) -> list[Planned]:
    """Hours at the desk without a break.

    Keyed by how many whole repeat-intervals have passed, so someone who keeps
    working is told again later rather than every minute — and someone who gets
    up and comes back starts from zero, because the stretch itself does.
    """
    minutes = round(moment.activity.current_stretch_s / 60)
    if not moment.present or minutes < schedule.long_session_minutes:
        return []

    over = (minutes - schedule.long_session_minutes) // schedule.long_session_repeat_minutes

    return [
        Planned(
            key=f"wellness:{moment.now.date().isoformat()}:{over}",
            notification=_notify(
                NotificationKind.WELLNESS,
                "Перерыв",
                f"Вы за компьютером уже {_hours_and_minutes(minutes)} без перерыва, сэр. "
                "Стоит размяться.",
                now=moment.now,
                schedule=schedule,
                priority=NotificationPriority.LOW,
            ),
        )
    ]


def _reminder_due(moment: Moment, schedule: Schedule) -> list[Planned]:
    """Something the owner asked to be told, at the time they asked.

    Said whether or not they are at the machine. Every other rule here checks
    presence first, because a briefing to an empty room is a briefing wasted —
    but this one was requested for a moment, and going quiet because nobody
    happened to be at the desk is exactly the failure that makes someone stop
    trusting reminders.
    """
    return [
        Planned(
            key=f"reminder:{identifier}",
            notification=_notify(
                NotificationKind.REMINDER,
                "Напоминание",
                f"Вы просили напомнить: {text}",
                now=moment.now,
                schedule=schedule,
                priority=NotificationPriority.HIGH,
            ),
        )
        for identifier, text in moment.due_reminders
    ]


def _prayer_due(moment: Moment, schedule: Schedule) -> list[Planned]:
    """A few minutes before each prayer.

    Not at the time itself. A reminder that arrives exactly at Maghrib is a
    reminder about something already happening; the point is the minutes before,
    which are what let someone finish what they are doing and go.

    Sunrise is skipped: it is a boundary rather than a prayer.
    """
    if not moment.present or moment.prayers is None or schedule.prayer_reminder_minutes <= 0:
        return []

    window = timedelta(minutes=schedule.prayer_reminder_minutes)
    planned: list[Planned] = []

    for prayer, at in moment.prayers.prayers():
        if not (moment.now <= at <= moment.now + window):
            continue
        minutes = max(1, round((at - moment.now).total_seconds() / 60))
        planned.append(
            Planned(
                key=f"prayer:{prayer.value}:{moment.now.date().isoformat()}",
                notification=_notify(
                    NotificationKind.PRAYER,
                    prayer.russian,
                    f"Через {minutes} мин — {prayer.russian}, в {at.strftime('%H:%M')}.",
                    now=moment.now,
                    schedule=schedule,
                ),
            )
        )

    return planned


def _count(total: int) -> str:
    """ "одна задача", "три задачи", "одиннадцать задач" — spoken, not printed."""
    if total % 10 == 1 and total % 100 != 11:
        return f"{total} задача"
    if total % 10 in (2, 3, 4) and total % 100 not in (12, 13, 14):
        return f"{total} задачи"
    return f"{total} задач"


#: In the order they would be said if several were due at once, which is also
#: the order of how much they matter.
RULES = (
    _reminder_due,
    _prayer_due,
    _due_soon,
    _morning_briefing,
    _long_session,
    _evening_summary,
)


def decide_all(moment: Moment, schedule: Schedule, *, already_said: set[str]) -> list[Planned]:
    """Everything worth saying at this moment and not said already."""
    planned: list[Planned] = []
    for rule in RULES:
        for candidate in rule(moment, schedule):
            if candidate.key not in already_said:
                planned.append(candidate)
    return planned
