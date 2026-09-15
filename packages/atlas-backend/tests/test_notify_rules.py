"""When the assistant should speak first, and — mostly — when it should not.

These rules are the only code in the project that acts without being asked, so
the tests are written the other way up from usual: the interesting assertions
are the silences. An assistant that says nothing useful is disappointing; one
that says something at the wrong moment gets muted, and after that none of the
rest of this matters.

Nothing here touches a clock, a database or a network. A rule takes a made-up
Tuesday afternoon and returns sentences, which is what makes "what happens at
23:40 when a task is due" a thing that can be checked at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atlas_backend.activity.summary import ActivityDigest
from atlas_backend.notify.rules import Moment, Schedule, decide_all
from atlas_backend.tracker.provider import Priority, Task
from atlas_shared.enums import NotificationKind, NotificationPriority

SCHEDULE = Schedule()

#: A Tuesday, mid-afternoon: outside quiet hours, past the briefing hour, before
#: the evening one. Every test that is not about the clock starts here.
AFTERNOON = datetime(2026, 9, 15, 14, 30, tzinfo=UTC)


def task(title: str, *, at: str | None = None, done: bool = False, identifier: str = "") -> Task:
    return Task(
        id=identifier or title.lower(),
        title=title,
        when=AFTERNOON,
        at=at,
        done=done,
        priority=Priority.MEDIUM,
    )


def moment(**kwargs: object) -> Moment:
    kwargs.setdefault("now", AFTERNOON)
    kwargs.setdefault("present", True)
    return Moment(**kwargs)  # type: ignore[arg-type]


def kinds(planned: list) -> list[NotificationKind]:  # type: ignore[type-arg]
    return [item.notification.kind for item in planned]


def decide(moment_: Moment, said: set[str] | None = None, schedule: Schedule = SCHEDULE) -> list:  # type: ignore[type-arg]
    return decide_all(moment_, schedule, already_said=said if said is not None else set())


class TestSayingNothing:
    def test_an_ordinary_afternoon_with_nothing_due_is_silent(self) -> None:
        """The default answer, and the one that has to stay the default."""
        assert decide(moment(tasks=[task("Read a book")])) == []

    def test_nothing_is_said_to_an_empty_chair(self) -> None:
        """The agent stays connected through a lunch break. Spending the
        once-a-day briefing on an empty room loses it for the day."""
        soon = (AFTERNOON + timedelta(minutes=10)).strftime("%H:%M")

        assert decide(moment(tasks=[task("Созвон", at=soon)], present=False)) == []

    def test_a_finished_task_is_not_announced(self) -> None:
        soon = (AFTERNOON + timedelta(minutes=10)).strftime("%H:%M")

        assert decide(moment(tasks=[task("Тренировка", at=soon, done=True)])) == []

    def test_something_far_off_is_not_a_reminder_yet(self) -> None:
        later = (AFTERNOON + timedelta(hours=3)).strftime("%H:%M")

        assert decide(moment(tasks=[task("Созвон", at=later)])) == []

    def test_something_already_started_is_not_announced_as_upcoming(self) -> None:
        """ "In minus twenty minutes" is not a reminder. Once it has started the
        moment for a nudge has passed, and saying so is just noise."""
        past = (AFTERNOON - timedelta(minutes=20)).strftime("%H:%M")

        assert decide(moment(tasks=[task("Созвон", at=past)])) == []

    def test_a_task_with_no_hour_is_never_announced(self) -> None:
        """A task due today with no time is a thing to do, not an appointment.
        Announcing it at an arbitrary minute is an interruption the owner did
        not schedule."""
        assert decide(moment(tasks=[task("Позвонить в банк")])) == []

    def test_a_malformed_time_is_skipped_rather_than_crashing(self) -> None:
        """The hour is a string from someone else's database."""
        assert decide(moment(tasks=[task("Что-то", at="потом")])) == []

    def test_what_was_already_said_is_not_said_again(self) -> None:
        soon = (AFTERNOON + timedelta(minutes=10)).strftime("%H:%M")
        state = moment(tasks=[task("Созвон", at=soon, identifier="t1")])

        first = decide(state)
        assert kinds(first) == [NotificationKind.REMINDER]

        assert decide(state, said={first[0].key}) == []


class TestReminders:
    def test_something_starting_soon_is_announced_once(self) -> None:
        soon = (AFTERNOON + timedelta(minutes=10)).strftime("%H:%M")

        planned = decide(moment(tasks=[task("Тренировка", at=soon)]))

        assert kinds(planned) == [NotificationKind.REMINDER]
        assert "Тренировка" in planned[0].notification.body

    def test_it_says_how_long_there_is(self) -> None:
        """ "Soon" is not actionable. "In ten minutes" is."""
        soon = (AFTERNOON + timedelta(minutes=10)).strftime("%H:%M")

        planned = decide(moment(tasks=[task("Созвон", at=soon)]))

        assert "10 мин" in planned[0].notification.body

    def test_it_is_spoken(self) -> None:
        """The whole point. A reminder that waits to be noticed in a tray is a
        reminder about something that has already started."""
        soon = (AFTERNOON + timedelta(minutes=5)).strftime("%H:%M")

        planned = decide(moment(tasks=[task("Созвон", at=soon)]))

        assert planned[0].notification.speak is True

    def test_two_things_at_once_are_two_reminders(self) -> None:
        soon = (AFTERNOON + timedelta(minutes=10)).strftime("%H:%M")
        tasks = [
            task("Созвон", at=soon, identifier="a"),
            task("Тренировка", at=soon, identifier="b"),
        ]

        assert len(decide(moment(tasks=tasks))) == 2


class TestQuietHours:
    def test_at_midnight_a_reminder_is_shown_and_not_spoken(self) -> None:
        """Not suppressed: the information is still worth having in the morning.
        Just not announced aloud into a dark room."""
        midnight = AFTERNOON.replace(hour=23, minute=40)
        soon = (midnight + timedelta(minutes=10)).strftime("%H:%M")

        planned = decide(moment(now=midnight, tasks=[task("Дедлайн", at=soon)]))

        assert len(planned) == 1
        assert planned[0].notification.speak is False
        assert planned[0].notification.priority is NotificationPriority.LOW

    def test_the_briefing_is_not_spent_on_the_middle_of_the_night(self) -> None:
        """It is once a day. Firing it silently at 02:00 would mark it said and
        leave the real morning quiet."""
        night = AFTERNOON.replace(hour=2, minute=0)

        assert decide(moment(now=night, tasks=[task("Что-то")])) == []


class TestTheMorningBriefing:
    def test_it_says_what_the_day_holds(self) -> None:
        morning = AFTERNOON.replace(hour=9, minute=0)
        tasks = [task("Созвон", at="11:00", identifier="a"), task("Отчёт", identifier="b")]

        planned = decide(moment(now=morning, tasks=tasks))

        assert kinds(planned) == [NotificationKind.BRIEFING]
        assert "2 задачи" in planned[0].notification.body
        assert "Созвон" in planned[0].notification.body

    def test_an_empty_day_is_said_plainly_rather_than_skipped(self) -> None:
        """ "Nothing scheduled" is information. Silence is ambiguous — it could
        equally mean the tracker is down."""
        morning = AFTERNOON.replace(hour=9, minute=0)

        planned = decide(moment(now=morning, tasks=[]))

        assert kinds(planned) == [NotificationKind.BRIEFING]
        assert "ничего не запланировано" in planned[0].notification.body

    def test_it_does_not_arrive_before_the_hour(self) -> None:
        early = AFTERNOON.replace(hour=7, minute=30)

        assert decide(moment(now=early, tasks=[task("Что-то")])) == []

    def test_it_counts_in_words_a_person_would_use(self) -> None:
        """Eleven tasks read aloud as "11 задача" is the kind of thing that
        makes an assistant sound like a form."""
        morning = AFTERNOON.replace(hour=9, minute=0)

        one = decide(moment(now=morning, tasks=[task("A", identifier="a")]))
        five = decide(
            moment(now=morning, tasks=[task(f"T{i}", identifier=str(i)) for i in range(5)])
        )

        assert "1 задача" in one[0].notification.body
        assert "5 задач" in five[0].notification.body


class TestTheEveningSummary:
    def test_it_reports_the_day_at_the_machine(self) -> None:
        evening = AFTERNOON.replace(hour=21, minute=30)
        digest = ActivityDigest(active_s=3 * 3600 + 20 * 60)

        planned = decide(moment(now=evening, activity=digest, tasks=[]))

        assert kinds(planned) == [NotificationKind.SUMMARY]
        assert "3 ч 20 мин" in planned[0].notification.body

    def test_it_says_how_much_of_the_list_was_finished(self) -> None:
        evening = AFTERNOON.replace(hour=21, minute=30)
        tasks = [task("A", done=True, identifier="a"), task("B", identifier="b")]

        planned = decide(moment(now=evening, tasks=tasks))

        assert "Выполнено 1 из 2" in planned[0].notification.body

    def test_it_is_shown_rather_than_announced(self) -> None:
        """Nothing in a summary needs acting on tonight."""
        evening = AFTERNOON.replace(hour=21, minute=30)

        planned = decide(moment(now=evening, tasks=[]))

        assert planned[0].notification.priority is NotificationPriority.LOW
        assert planned[0].notification.speak is False


class TestTheLongSessionWarning:
    def test_two_hours_without_a_break_earns_a_word(self) -> None:
        digest = ActivityDigest(current_stretch_s=120 * 60)

        planned = decide(moment(activity=digest))

        assert kinds(planned) == [NotificationKind.WELLNESS]
        assert "2 ч" in planned[0].notification.body

    def test_an_hour_does_not(self) -> None:
        assert decide(moment(activity=ActivityDigest(current_stretch_s=60 * 60))) == []

    def test_someone_who_keeps_working_is_told_again_later(self) -> None:
        """Not every minute — the key changes once per repeat interval, so the
        nudge comes back at three hours rather than a hundred and twenty
        times."""
        first = decide(moment(activity=ActivityDigest(current_stretch_s=95 * 60)))
        said = {first[0].key}

        assert decide(moment(activity=ActivityDigest(current_stretch_s=100 * 60)), said) == []
        later = decide(moment(activity=ActivityDigest(current_stretch_s=155 * 60)), said)
        assert kinds(later) == [NotificationKind.WELLNESS]

    def test_getting_up_resets_it(self) -> None:
        """The stretch itself resets when the owner leaves, so the next warning
        is earned again rather than carried over."""
        assert decide(moment(activity=ActivityDigest(current_stretch_s=0, active_s=5 * 3600))) == []


class TestOrdering:
    def test_a_reminder_comes_before_a_briefing(self) -> None:
        """If both are due in the same minute, the thing about to start is the
        one that matters."""
        morning = AFTERNOON.replace(hour=9, minute=0)
        soon = (morning + timedelta(minutes=10)).strftime("%H:%M")

        planned = decide(moment(now=morning, tasks=[task("Созвон", at=soon)]))

        assert kinds(planned) == [NotificationKind.REMINDER, NotificationKind.BRIEFING]
