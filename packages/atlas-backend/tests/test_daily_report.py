"""The day, written down.

Two halves. The first is the prose — what the report actually says, which is
checked against a made-up day with no database anywhere near it. The second is
the filing: once, at the right hour, into the tracker, and not again.

The assertion that matters most is about a day nobody recorded. A report saying
"0 минут за компьютером" is a claim about the day; a report saying "нет данных"
is a claim about the record. Only the second one is true when the agent was
switched off, and the difference is the sort of thing that quietly rewrites
someone's memory of a week.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from atlas_backend.activity.summary import ActivityDigest
from atlas_backend.reports.daily import build_report
from atlas_backend.reports.writer import DailyReportWriter
from atlas_backend.tracker.provider import Goal, Habit, Priority, Task, TrackerError
from tests.conftest import insert_rows, requires_db

EVENING = datetime(2026, 9, 15, 22, 30, tzinfo=UTC)


def task(title: str, *, done: bool = False) -> Task:
    return Task(id=title.lower(), title=title, when=EVENING, done=done, priority=Priority.MEDIUM)


def habit(title: str, *, done: bool) -> Habit:
    return Habit(id=title.lower(), title=title, done_today=done, streak=3)


def report(**kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("on", EVENING)
    kwargs.setdefault("tasks", [])
    kwargs.setdefault("habits", [])
    kwargs.setdefault("activity", ActivityDigest())
    kwargs.setdefault("watched", True)
    return build_report(**kwargs)


# --------------------------------------------------------------- the prose


class TestWhatItSays:
    def test_it_is_titled_with_the_day_in_words(self) -> None:
        assert report().title == "Итоги дня — 15 сентября 2026"

    def test_it_counts_what_was_finished_and_names_what_was_not(self) -> None:
        """A number says how much is left; only a name says which, and that is
        the half that can still be acted on tomorrow."""
        body = report(
            tasks=[task("Тренировка", done=True), task("Отчёт"), task("Позвонить в банк")]
        ).as_body()

        assert "выполнено 1 из 3" in body
        assert "✓ Тренировка" in body
        assert "— Отчёт" in body
        assert "— Позвонить в банк" in body

    def test_an_empty_day_says_so_rather_than_saying_nothing(self) -> None:
        assert "ничего не было запланировано" in report().as_body()

    def test_it_reports_the_time_at_the_computer(self) -> None:
        digest = ActivityDigest(
            active_s=3 * 3600 + 20 * 60,
            idle_s=45 * 60,
            longest_stretch_s=95 * 60,
            by_app=(("VS Code", 2 * 3600), ("Chrome", 40 * 60)),
        )

        body = report(activity=digest).as_body()

        assert "3 ч 20 мин активно" in body
        assert "45 мин простоя" in body
        assert "VS Code — 2 ч" in body
        assert "Самый долгий заход без перерыва: 1 ч 35 мин." in body

    def test_a_day_nobody_recorded_says_there_is_no_data(self) -> None:
        """Not "0 минут". That is a claim about the day rather than about the
        record, and it is false — the agent was simply not running."""
        body = report(watched=False).as_body()

        assert "нет данных" in body
        assert "0 мин" not in body

    def test_habits_name_only_what_was_missed(self) -> None:
        body = report(habits=[habit("Вода", done=True), habit("Чтение", done=False)]).as_body()

        assert "Привычки: 1 из 2." in body
        assert "Пропущено: Чтение." in body

    def test_habits_are_left_out_entirely_when_there_are_none(self) -> None:
        assert "Привычки" not in report().as_body()

    def test_it_is_tagged_so_it_can_be_found_and_filtered_out(self) -> None:
        assert report().tags == ("jarvis", "итоги-дня")

    def test_only_a_handful_of_applications_are_named(self) -> None:
        """A report is glanced at. Ten lines of application names is a log."""
        digest = ActivityDigest(
            active_s=8 * 3600,
            by_app=tuple((f"App {index}", 3600.0) for index in range(9)),
        )

        assert report(activity=digest).as_body().count(" — ") == 5

    def test_a_barely_touched_application_is_not_worth_a_line(self) -> None:
        digest = ActivityDigest(active_s=3600, by_app=(("VS Code", 3500.0), ("Explorer", 20.0)))

        assert "Explorer" not in report(activity=digest).as_body()


# --------------------------------------------------------------- the filing


class Filed:
    """Stands in for the tracker's note API."""

    def __init__(self, *, broken: bool = False) -> None:
        self.notes: list[tuple[str, str, list[str]]] = []
        self._broken = broken

    async def add_note(self, *, title: str, body: str, tags: Sequence[str] = ()) -> str:
        if self._broken:
            raise TrackerError("the tracker is not answering")
        self.notes.append((title, body, list(tags)))
        return "note_1"


class FakeTracker:
    name = "fake"

    def __init__(self, tasks: list[Task] | None = None, *, broken: bool = False) -> None:
        self._tasks = tasks or []
        self._broken = broken

    async def tasks_today(self) -> list[Task]:
        if self._broken:
            raise TrackerError("the tracker is not answering")
        return self._tasks

    async def tasks_upcoming(self, *, days: int = 7) -> list[Task]:
        return self._tasks

    async def goals(self) -> list[Goal]:
        return []

    async def habits(self) -> list[Habit]:
        if self._broken:
            raise TrackerError("the tracker is not answering")
        return [habit("Вода", done=True)]


def writer(settings, **kwargs):  # type: ignore[no-untyped-def]
    from atlas_backend.db.session import Database

    kwargs.setdefault("hour", 22)
    return DailyReportWriter(database=Database(settings), **kwargs)


@pytest.mark.integration
@requires_db
class TestFilingIt:
    async def test_it_writes_one_note_after_the_hour(self, settings) -> None:  # type: ignore[no-untyped-def]
        filed = Filed()
        engine = writer(settings, tracker=FakeTracker([task("Отчёт", done=True)]), notes=filed)

        written = await engine.maybe_write(EVENING)

        assert written is not None
        assert len(filed.notes) == 1
        title, body, tags = filed.notes[0]
        assert title == "Итоги дня — 15 сентября 2026"
        assert "выполнено 1 из 1" in body
        assert tags == ["jarvis", "итоги-дня"]

    async def test_nothing_happens_before_the_hour(self, settings) -> None:  # type: ignore[no-untyped-def]
        filed = Filed()
        engine = writer(settings, tracker=FakeTracker(), notes=filed)

        assert await engine.maybe_write(EVENING.replace(hour=18)) is None
        assert filed.notes == []

    async def test_it_is_written_once_however_often_the_loop_runs(self, settings) -> None:  # type: ignore[no-untyped-def]
        """This is checked every minute from ten in the evening to midnight."""
        filed = Filed()
        engine = writer(settings, tracker=FakeTracker(), notes=filed)

        await engine.maybe_write(EVENING)
        await engine.maybe_write(EVENING + timedelta(minutes=1))
        await engine.maybe_write(EVENING + timedelta(hours=1))

        assert len(filed.notes) == 1

    async def test_tomorrow_gets_its_own(self, settings) -> None:  # type: ignore[no-untyped-def]
        filed = Filed()
        engine = writer(settings, tracker=FakeTracker(), notes=filed)

        await engine.maybe_write(EVENING)
        await engine.maybe_write(EVENING + timedelta(days=1))

        assert len(filed.notes) == 2

    async def test_a_tracker_that_cannot_be_written_to_is_not_retried_all_night(
        self,
        settings,  # type: ignore[no-untyped-def]
    ) -> None:
        """One failed report, not a hundred and twenty attempts."""
        filed = Filed(broken=True)
        engine = writer(settings, tracker=FakeTracker(), notes=filed)

        assert await engine.maybe_write(EVENING) is None
        assert await engine.maybe_write(EVENING + timedelta(minutes=1)) is None

    async def test_with_nowhere_to_file_it_nothing_is_attempted(self, settings) -> None:  # type: ignore[no-untyped-def]
        """No tracker configured. The report has no home, and pretending
        otherwise would mean building one every minute and dropping it."""
        engine = writer(settings, tracker=None)

        assert engine.available is False
        assert await engine.maybe_write(EVENING) is None

    async def test_a_tracker_that_is_down_still_produces_the_computer_half(
        self,
        settings,  # type: ignore[no-untyped-def]
    ) -> None:
        """Sunny unreachable at ten in the evening should not lose the record of
        the day at the machine, which is stored here and cannot be recovered
        later."""
        filed = Filed()
        engine = writer(settings, tracker=FakeTracker(broken=True), notes=filed)

        written = await engine.maybe_write(EVENING)

        assert written is not None
        assert "За компьютером" in filed.notes[0][1]


@pytest.mark.integration
@requires_db
class TestTheComputerHalf:
    async def test_the_hours_come_from_the_samples(self, settings) -> None:  # type: ignore[no-untyped-def]
        device_id = uuid.uuid4()
        await _record(device_id, minutes=45, until=EVENING)
        filed = Filed()

        await writer(settings, tracker=FakeTracker(), notes=filed).maybe_write(EVENING)

        assert "45 мин активно" in filed.notes[0][1]

    async def test_a_day_with_no_samples_is_reported_as_unwatched(self, settings) -> None:  # type: ignore[no-untyped-def]
        filed = Filed()

        await writer(settings, tracker=FakeTracker(), notes=filed).maybe_write(EVENING)

        assert "нет данных" in filed.notes[0][1]

    async def test_two_machines_do_not_have_their_days_added_together(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Merging timelines would double-count an afternoon. The busier machine
        is the one reported on; the other is simply absent."""
        busy, quiet = uuid.uuid4(), uuid.uuid4()
        await _record(busy, minutes=120, until=EVENING)
        await _record(quiet, minutes=20, until=EVENING)
        filed = Filed()

        await writer(settings, tracker=FakeTracker(), notes=filed).maybe_write(EVENING)

        assert "2 ч активно" in filed.notes[0][1]


async def _record(device_id: uuid.UUID, *, minutes: float, until: datetime) -> None:
    start = until - timedelta(minutes=minutes)
    await insert_rows(
        "INSERT INTO activity_samples (device_id, ts, process_name, is_idle, idle_seconds) "
        "VALUES (:device_id, :ts, :process_name, :is_idle, :idle_seconds)",
        [
            {
                "device_id": str(device_id),
                "ts": start + timedelta(seconds=index * 10),
                "process_name": "Code.exe",
                "is_idle": False,
                "idle_seconds": 0,
            }
            for index in range(int(minutes * 6) + 1)
        ],
    )


class TestRemindersStillWaiting:
    """The one thing in the report the owner might still act on tonight."""

    def test_they_are_named(self) -> None:
        body = report(reminders_waiting=["позвонить маме", "выключить плиту"]).as_body()

        assert "Напоминания, которые ещё не прозвучали: 2." in body
        assert "позвонить маме" in body

    def test_none_waiting_leaves_the_section_out(self) -> None:
        assert "Напоминания" not in report().as_body()
