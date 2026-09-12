"""Whether a spoken tracker answer is worth listening to.

The requirement is one sentence: *"Сегодня у вас 11 задач, сэр. Три высокого
приоритета. Ближайшая — тренировка в 15:00. Хотите услышать остальные?"* —
and everything here exists because that cannot be achieved by asking the model
to be brief. The orchestrator hands the entire tool result back to the model, so
what this returns is what the model has read. Eleven titles in the result are
eleven titles it may recite.

So the guarantee is structural: past a small number, the titles are not there to
recite. These tests are the guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from atlas_backend.tracker.digest import NAMED_INDIVIDUALLY, summarise
from atlas_backend.tracker.provider import Priority, Task

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def task(
    title: str,
    *,
    priority: Priority = Priority.MEDIUM,
    hours: float | None = None,
    at: str | None = None,
    done: bool = False,
) -> Task:
    deadline = NOW + timedelta(hours=hours) if hours is not None else None
    return Task(
        id=title.lower().replace(" ", "-"),
        title=title,
        priority=priority,
        deadline=deadline,
        at=at,
        done=done,
    )


def many(count: int) -> list[Task]:
    return [task(f"Task {index}") for index in range(count)]


class TestWhenThereAreFew:
    def test_nothing_at_all_says_nothing_at_all(self) -> None:
        digest = summarise([], now=NOW)

        assert digest.total == 0
        assert digest.items == []
        assert digest.as_result() == {"total": 0}

    def test_a_handful_is_named_one_by_one(self) -> None:
        """Counting three things is worse than saying them."""
        digest = summarise([task("Gym"), task("Email Anna")], now=NOW)

        assert [item["title"] for item in digest.items] == ["Gym", "Email Anna"]
        assert digest.more == 0

    def test_an_overview_and_a_listing_are_different_questions(self) -> None:
        """One asks what the day looks like, the other asks for names. Keying
        them on whether an offset is zero made the second unreachable."""
        overview = summarise(many(11), now=NOW)
        listing = summarise(many(11), now=NOW, offset=0)

        assert overview.items == []
        assert overview.by_priority
        assert listing.items
        assert listing.by_priority == {}

    def test_a_short_list_is_not_summarised_into_arithmetic(self) -> None:
        digest = summarise(many(NAMED_INDIVIDUALLY), now=NOW)

        assert len(digest.items) == NAMED_INDIVIDUALLY
        assert digest.by_priority == {}
        assert digest.next_up is None


class TestWhenThereAreMany:
    def test_eleven_tasks_do_not_arrive_as_eleven_titles(self) -> None:
        """The requirement, stated as a test. A result carrying all eleven is a
        model that will read all eleven, however the prompt is worded."""
        result = summarise(many(11), now=NOW).as_result()

        assert result["total"] == 11
        assert "items" not in result or len(result["items"]) <= NAMED_INDIVIDUALLY
        assert len(str(result)) < 400

    def test_it_says_how_many_and_how_urgent(self) -> None:
        tasks = [
            *[task(f"High {i}", priority=Priority.HIGH) for i in range(3)],
            *[task(f"Medium {i}") for i in range(6)],
            *[task(f"Low {i}", priority=Priority.LOW) for i in range(2)],
        ]

        digest = summarise(tasks, now=NOW)

        assert digest.total == 11
        assert digest.by_priority == {"low": 2, "medium": 6, "high": 3}

    def test_empty_priorities_are_left_out(self) -> None:
        """ "Zero urgent" costs a clause and tells nobody anything."""
        digest = summarise(many(8), now=NOW)

        assert "urgent" not in digest.by_priority
        assert "high" not in digest.by_priority

    def test_the_next_thing_is_the_soonest_with_a_time(self) -> None:
        tasks = [
            task("Report", hours=6, at="18:00"),
            task("Training", hours=3, at="15:00"),
            task("Sometime today", hours=1),
            *many(5),
        ]

        digest = summarise(tasks, now=NOW)

        assert digest.next_up == {"title": "Training", "at": "15:00"}

    def test_a_deadline_without_a_time_is_not_next(self) -> None:
        """A day is not a moment, and "what's next" asks about moments."""
        digest = summarise([task("Someday", hours=1), *many(5)], now=NOW)

        assert digest.next_up is None

    def test_something_already_done_is_not_next(self) -> None:
        tasks = [task("Finished", hours=1, at="13:00", done=True), *many(5)]

        assert summarise(tasks, now=NOW).next_up is None

    def test_being_late_is_counted_because_it_changes_the_answer(self) -> None:
        tasks = [task("Late", hours=-3), task("Later still", hours=-1), *many(5)]

        assert summarise(tasks, now=NOW).overdue == 2

    def test_a_finished_task_is_not_late(self) -> None:
        tasks = [task("Done late", hours=-3, done=True), *many(5)]

        assert summarise(tasks, now=NOW).overdue == 0


class TestHearingTheRest:
    def test_the_model_is_told_how_to_continue(self) -> None:
        """Otherwise "хотите услышать остальные?" is an offer it cannot keep."""
        result = summarise(many(11), now=NOW).as_result()

        assert result["more"] == 11
        assert "offset=" in result["hint"]

    def test_asking_again_returns_the_next_few_by_name(self) -> None:
        """The hint the overview gives must lead somewhere.

        It once pointed at offset zero, which returned the overview again —
        there was no route from "eleven tasks" to hearing any of their names.
        """
        overview = summarise(many(11), now=NOW)
        first = overview.as_result()["hint"]
        assert "offset=0" in first

        following = summarise(many(11), now=NOW, offset=0)

        assert [item["title"] for item in following.items] == ["Task 0", "Task 1", "Task 2"]

    def test_a_follow_up_does_not_repeat_what_was_said(self) -> None:
        following = summarise(many(11), now=NOW, offset=3)

        assert [item["title"] for item in following.items] == ["Task 3", "Task 4", "Task 5"]

    def test_the_end_of_the_list_offers_nothing_more(self) -> None:
        following = summarise(many(6), now=NOW, offset=3)

        assert following.more == 0
        assert "hint" not in following.as_result()

    def test_an_offset_past_the_end_is_empty_rather_than_an_error(self) -> None:
        """The model can miscount, and an exception here would surface as the
        assistant apologising for something nobody noticed."""
        following = summarise(many(5), now=NOW, offset=99)

        assert following.items == []
        assert following.total == 5

    def test_the_total_is_the_whole_list_not_the_remainder(self) -> None:
        """Otherwise a follow-up reports a smaller day than the first answer."""
        assert summarise(many(11), now=NOW, offset=6).total == 11


class TestWhatTheModelActuallySees:
    def test_only_what_can_be_spoken_survives(self) -> None:
        digest = summarise([task("Gym", priority=Priority.HIGH, hours=3, at="15:00")], now=NOW)

        assert digest.items[0] == {"title": "Gym", "priority": "high", "at": "15:00"}

    def test_an_untimed_task_carries_no_empty_time(self) -> None:
        digest = summarise([task("Read")], now=NOW)

        assert "at" not in digest.items[0]

    @pytest.mark.parametrize("count", [0, 1, 3, 4, 11, 50, 200])
    def test_the_result_stays_small_whatever_the_day_looks_like(self, count: int) -> None:
        """A tracker with two hundred tasks is the case where this matters most,
        and the one least likely to be tried by hand."""
        result = summarise(many(count), now=NOW).as_result()

        assert len(str(result)) < 500
