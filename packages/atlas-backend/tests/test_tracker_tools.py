"""Running a tracker tool, with a tracker that is only a stand-in.

What is under test is the bridge: a validated call goes in, one provider method
is called, and what comes back is shaped for the model to speak. The provider
is faked because whether Sunny answers is Sunny's problem and is covered in
test_tracker_sunny.py.

The property worth guarding is negative. There is no handler that takes a path,
no dispatch on a string the model wrote, and a tool absent from the table cannot
be run whatever the catalogue claims.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from atlas_backend.tracker.provider import Applied, Goal, Habit, Priority, Task, TrackerError
from atlas_backend.tracker.tools import TRACKER_TOOLS, run_tracker_tool
from atlas_shared.tools.catalog import CATALOG

NOW = datetime.now(UTC)


class FakeTracker:
    """Records what it was asked, answers with whatever it was given."""

    name = "fake"

    def __init__(
        self,
        *,
        tasks: list[Task] | None = None,
        goals: list[Goal] | None = None,
        habits: list[Habit] | None = None,
    ) -> None:
        self._tasks = tasks or []
        self._goals = goals or []
        self._habits = habits or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def tasks_today(self) -> list[Task]:
        self.calls.append(("tasks_today", {}))
        return self._tasks

    async def tasks_upcoming(self, *, days: int = 7) -> list[Task]:
        self.calls.append(("tasks_upcoming", {"days": days}))
        return self._tasks

    async def goals(self) -> list[Goal]:
        self.calls.append(("goals", {}))
        return self._goals

    async def habits(self) -> list[Habit]:
        self.calls.append(("habits", {}))
        return self._habits

    async def add_task(
        self,
        *,
        title: str,
        priority: Priority = Priority.MEDIUM,
        when: datetime | None = None,
    ) -> Applied:
        self.calls.append(("add_task", {"title": title, "priority": priority, "when": when}))
        return Applied(what=title, detail="added")

    async def add_goal(self, *, title: str, target_date: date | None = None) -> Applied:
        self.calls.append(("add_goal", {"title": title, "target_date": target_date}))
        return Applied(what=title, detail="added")

    async def complete_task(self, *, task_id: str) -> Applied:
        self.calls.append(("complete_task", {"task_id": task_id}))
        return Applied(what="Тренировка", detail="completed")

    async def reschedule_task(self, *, task_id: str, when: datetime) -> Applied:
        self.calls.append(("reschedule_task", {"task_id": task_id, "when": when}))
        return Applied(what="Отчёт", detail="moved to 2026-09-20 09:00")

    async def set_priority(self, *, task_id: str, priority: Priority) -> Applied:
        self.calls.append(("set_priority", {"task_id": task_id, "priority": priority}))
        return Applied(what="Отчёт", detail=f"priority {priority.value}")


def task(title: str, *, at: str | None = None, hours: float = 2, done: bool = False) -> Task:
    return Task(
        id=title.lower(),
        title=title,
        when=NOW.replace(microsecond=0),
        at=at,
        done=done,
    )


class TestTheSurface:
    def test_every_tracker_tool_in_the_catalogue_has_a_handler(self) -> None:
        """A catalogue entry with nothing behind it is a tool the model will
        propose and the assistant will fail to run, which reads to the owner as
        the assistant being broken."""
        declared = {name for name in CATALOG.names() if name.startswith("tracker.")}

        assert declared == set(TRACKER_TOOLS)

    def test_every_handler_has_a_catalogue_entry(self) -> None:
        """The other direction: a handler nothing can reach is dead code that
        still looks like a feature."""
        assert set(TRACKER_TOOLS) <= CATALOG.names()

    async def test_an_unknown_tool_is_refused_rather_than_crashing(self) -> None:
        with pytest.raises(TrackerError, match="not something the tracker can do"):
            await run_tracker_tool(FakeTracker(), "tracker.delete_everything", {})

    def test_there_is_no_delete(self) -> None:
        """Sunny has one. This does not, and the reason is measured: recognition
        in this project has turned an open into a close."""
        assert not any("delete" in name for name in TRACKER_TOOLS)


class TestReading:
    async def test_today_returns_a_digest_not_a_list(self) -> None:
        tracker = FakeTracker(tasks=[task(f"Task {i}") for i in range(11)])

        result = await run_tracker_tool(tracker, "tracker.today", {})

        assert result["total"] == 11
        assert "items" not in result
        assert result["more"] == 11

    async def test_an_offset_names_them(self) -> None:
        tracker = FakeTracker(tasks=[task(f"Task {i}") for i in range(11)])

        result = await run_tracker_tool(tracker, "tracker.today", {"offset": 0})

        assert [item["title"] for item in result["items"]] == ["Task 0", "Task 1", "Task 2"]

    async def test_upcoming_passes_the_horizon_through(self) -> None:
        tracker = FakeTracker(tasks=[task("Soon")])

        result = await run_tracker_tool(tracker, "tracker.upcoming", {"days": 14})

        assert tracker.calls[0] == ("tasks_upcoming", {"days": 14})
        assert result["days"] == 14

    async def test_a_schedule_is_only_the_things_with_a_time(self) -> None:
        """Something due today with no hour is a thing to do, not an
        appointment. Including it makes the answer longer and less like a
        schedule."""
        tracker = FakeTracker(
            tasks=[task("Gym", at="15:00"), task("Read"), task("Call", at="09:30")]
        )

        result = await run_tracker_tool(tracker, "tracker.schedule", {})

        assert [item["title"] for item in result["items"]] == ["Call", "Gym"]

    async def test_a_schedule_names_its_items_rather_than_counting_them(self) -> None:
        """Asked for a schedule against four real appointments, the first
        version answered "four tasks, two of them high priority" — true, and
        not a schedule. A sequence answers "what does my day look like"; an
        inventory answers "how much is there"."""
        tracker = FakeTracker(
            tasks=[task(f"Meeting {index}", at=f"{9 + index:02d}:00") for index in range(6)]
        )

        result = await run_tracker_tool(tracker, "tracker.schedule", {})

        assert result["items"], "a schedule that names nothing is not a schedule"
        assert result["items"][0]["title"] == "Meeting 0"

    async def test_the_day_is_read_in_the_order_it_happens(self) -> None:
        """The tracker returns whatever its board is arranged by, and read
        aloud that came out 22:30, then 21:45, then 20:30. Backwards through
        the evening is not something a listener can follow."""
        tracker = FakeTracker(
            tasks=[
                task("Reading", at="22:30"),
                task("Python", at="20:30"),
                task("Family", at="19:00"),
                task("IELTS", at="21:45"),
            ]
        )

        result = await run_tracker_tool(tracker, "tracker.today", {"offset": 0})

        assert [item["at"] for item in result["items"]] == ["19:00", "20:30", "21:45"]

    async def test_untimed_work_comes_after_the_appointments(self) -> None:
        """It is not part of the sequence, so it does not interrupt it."""
        tracker = FakeTracker(tasks=[task("Someday"), task("Gym", at="09:00")])

        result = await run_tracker_tool(tracker, "tracker.today", {"offset": 0})

        assert [item["title"] for item in result["items"]] == ["Gym", "Someday"]

    async def test_a_finished_appointment_is_not_in_the_schedule(self) -> None:
        tracker = FakeTracker(tasks=[task("Gym", at="15:00", done=True)])

        assert await run_tracker_tool(tracker, "tracker.schedule", {}) == {"total": 0}

    async def test_goals_are_named_because_there_are_few(self) -> None:
        tracker = FakeTracker(goals=[Goal(id="g1", title="Learn Spanish", progress=40)])

        result = await run_tracker_tool(tracker, "tracker.goals", {})

        assert result["goals"] == [{"title": "Learn Spanish", "progress": 40}]

    async def test_habits_report_what_is_left_rather_than_what_is_done(self) -> None:
        """What is finished needs no names; what is outstanding is the half that
        can still be acted on."""
        tracker = FakeTracker(
            habits=[
                Habit(id="h1", title="Water", done_today=True, streak=12),
                Habit(id="h2", title="Reading", done_today=False, streak=3),
            ]
        )

        result = await run_tracker_tool(tracker, "tracker.habits", {})

        assert result == {
            "total": 2,
            "done_today": 1,
            "outstanding": ["Reading"],
            "best_streak": 12,
        }


class TestWriting:
    async def test_adding_a_task_passes_the_priority_through(self) -> None:
        tracker = FakeTracker()

        result = await run_tracker_tool(
            tracker, "tracker.add_task", {"title": "Call the bank", "priority": "high"}
        )

        assert tracker.calls[0][1]["priority"] is Priority.HIGH
        assert result == {"added": "Call the bank"}

    async def test_a_deadline_is_parsed_rather_than_forwarded_as_text(self) -> None:
        tracker = FakeTracker()

        await run_tracker_tool(
            tracker,
            "tracker.add_task",
            {"title": "Report", "when": "2026-09-20T09:00:00Z"},
        )

        assert tracker.calls[0][1]["when"] == datetime(2026, 9, 20, 9, 0, tzinfo=UTC)

    async def test_a_date_the_model_invented_is_refused_by_name(self) -> None:
        """Models write dates as prose more often than anyone expects. Failing
        here with the field named lets the assistant ask again; failing inside
        the HTTP call tells the owner the tracker is broken."""
        with pytest.raises(TrackerError, match="when is not a date"):
            await run_tracker_tool(
                FakeTracker(), "tracker.add_task", {"title": "x", "when": "next Tuesday"}
            )

    async def test_a_naive_timestamp_is_treated_as_utc_rather_than_refused(self) -> None:
        tracker = FakeTracker()

        await run_tracker_tool(
            tracker, "tracker.add_task", {"title": "x", "when": "2026-09-20T09:00:00"}
        )

        assert tracker.calls[0][1]["when"].tzinfo is not None

    async def test_completing_reports_which_task(self) -> None:
        """ "Done" is not an answer when the question was which one, and a
        misheard title that got this far has to reach the owner's ears."""
        result = await run_tracker_tool(FakeTracker(), "tracker.complete_task", {"task_id": "t1"})

        assert result == {"completed": "Тренировка"}

    async def test_rescheduling_reports_the_new_time(self) -> None:
        result = await run_tracker_tool(
            FakeTracker(),
            "tracker.reschedule_task",
            {"task_id": "t1", "when": "2026-09-20T09:00:00Z"},
        )

        assert result["moved"] == "Отчёт"
        assert "2026-09-20" in result["to"]

    async def test_priority_reports_both_the_task_and_the_level(self) -> None:
        result = await run_tracker_tool(
            FakeTracker(), "tracker.set_priority", {"task_id": "t1", "priority": "urgent"}
        )

        assert result == {"changed": "Отчёт", "priority": "urgent"}


class TestArgumentsAgainstTheManifest:
    """The handlers trust their arguments because the manifest checked them.

    These make sure the manifest really does, since the trust is what allows
    the handlers to be as plain as they are.
    """

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("tracker.add_task", {"title": "x", "priority": "extremely"}),
            ("tracker.set_priority", {"task_id": "t", "priority": "sort of"}),
            ("tracker.add_task", {"title": ""}),
            ("tracker.complete_task", {"task_id": ""}),
            ("tracker.upcoming", {"days": 0}),
            ("tracker.upcoming", {"days": 9999}),
            ("tracker.today", {"offset": -1}),
        ],
    )
    def test_bad_arguments_never_reach_a_handler(self, tool: str, args: dict[str, Any]) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CATALOG.get(tool).validate_args(args)

    def test_an_unexpected_argument_is_refused(self) -> None:
        """Extra fields are forbidden across the catalogue, which is what stops
        a model smuggling something past a handler that ignores what it does
        not recognise."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CATALOG.get("tracker.today").validate_args({"url": "https://example.com"})
