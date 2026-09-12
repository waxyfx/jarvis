"""Talking to Sunny, without Sunny.

A mock transport rather than a running Next.js app: what is under test is the
mapping from an action to a request, and whether an unexpected answer degrades
or explodes. Whether Sunny's own endpoints work is Sunny's business and Sunny's
tests.

Two things here are not ordinary coverage and are the reason the file exists.
The token must never appear anywhere it could be read back — not in an error, not
in a log, not in a URL. And the request paths must be fixed strings, because the
whole security argument for this integration is that no function exists which
would turn model output into an arbitrary HTTP call.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from atlas_backend.tracker.provider import (
    Priority,
    TrackerError,
    TrackerUnavailableError,
)
from atlas_backend.tracker.sunny import SunnyTracker

TOKEN = "a-token-long-enough-to-be-accepted"


def tracker(handler: Any, *, token: str = TOKEN) -> SunnyTracker:
    return SunnyTracker(
        base_url="https://sunny.example",
        token=token,
        transport=httpx.MockTransport(handler),
    )


def replying(payload: Any, status: int = 200) -> Any:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


class TestConfiguration:
    def test_no_url_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(TrackerUnavailableError, match="no tracker URL"):
            SunnyTracker(base_url="", token=TOKEN)

    def test_a_token_too_short_to_be_one_is_refused_here(self) -> None:
        """Sunny refuses anything under 24 characters on its own side. Failing
        here says so while the person is still reading the message."""
        with pytest.raises(TrackerUnavailableError, match="too short"):
            SunnyTracker(base_url="https://sunny.example", token="short")

    def test_a_trailing_slash_does_not_become_a_double_slash(self) -> None:
        handler = replying([])
        client = SunnyTracker(
            base_url="https://sunny.example/",
            token=TOKEN,
            transport=httpx.MockTransport(handler),
        )

        import asyncio

        asyncio.run(client.tasks_today())

        assert "//api" not in str(handler.seen[0].url)


class TestReading:
    async def test_todays_tasks_ask_sunny_for_todays_scope(self) -> None:
        """`scope` is Sunny's own vocabulary, not something invented here."""
        handler = replying([])

        await tracker(handler).tasks_today()

        request = handler.seen[0]
        assert request.method == "GET"
        assert request.url.path == "/api/tasks"
        assert request.url.params["scope"] == "today"

    async def test_a_task_keeps_only_what_can_be_spoken(self) -> None:
        """Subtasks, comments, attachments and tags are dropped here rather
        than downstream: whatever survives is what the model reads."""
        handler = replying(
            [
                {
                    "id": "t1",
                    "title": "  Тренировка  ",
                    "priority": "high",
                    "deadline": "2026-09-12T15:00:00Z",
                    "subtasks": [{"id": "s1"}],
                    "comments": [{"id": "c1"}],
                    "tags": ["gym"],
                    "project": {"id": "p1", "name": "Health"},
                }
            ]
        )

        tasks = await tracker(handler).tasks_today()

        assert tasks[0].title == "Тренировка"
        assert tasks[0].priority is Priority.HIGH
        assert tasks[0].at == "15:00"
        assert tasks[0].project == "Health"

    async def test_a_deadline_without_a_time_carries_no_time(self) -> None:
        handler = replying([{"id": "t1", "title": "Read", "deadline": "2026-09-12T00:00:00Z"}])

        assert (await tracker(handler).tasks_today())[0].at is None

    async def test_an_unparseable_date_costs_the_date_and_nothing_else(self) -> None:
        """An unexpected shape should cost one missing deadline, not an
        exception halfway through reading the day aloud."""
        handler = replying([{"id": "t1", "title": "Read", "deadline": "not a date"}])

        tasks = await tracker(handler).tasks_today()

        assert tasks[0].title == "Read"
        assert tasks[0].deadline is None

    async def test_an_unknown_priority_falls_back_rather_than_failing(self) -> None:
        handler = replying([{"id": "t1", "title": "Read", "priority": "extremely"}])

        assert (await tracker(handler).tasks_today())[0].priority is Priority.MEDIUM

    @pytest.mark.parametrize("key", ["items", "tasks", "data"])
    async def test_a_wrapped_list_is_accepted_as_well_as_a_bare_one(self, key: str) -> None:
        handler = replying({key: [{"id": "t1", "title": "Read"}]})

        assert len(await tracker(handler).tasks_today()) == 1

    async def test_something_that_is_not_a_list_at_all_is_empty(self) -> None:
        """Better an empty day than a stack trace during a spoken answer."""
        handler = replying({"unexpected": True})

        assert await tracker(handler).tasks_today() == []

    async def test_upcoming_tasks_beyond_the_horizon_are_dropped(self) -> None:
        far = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        handler = replying(
            [
                {"id": "a", "title": "Soon", "deadline": far},
                {"id": "b", "title": "Next year", "deadline": "2030-01-01T10:00:00Z"},
            ]
        )

        titles = [task.title for task in await tracker(handler).tasks_upcoming(days=7)]

        assert titles == ["Soon"]

    async def test_goals_and_habits_come_back_reduced_too(self) -> None:
        goals = replying([{"id": "g1", "title": "Learn Spanish", "progress": 40}])
        habits = replying([{"id": "h1", "title": "Water", "doneToday": True, "streak": 12}])

        assert (await tracker(goals).goals())[0].progress == 40
        habit = (await tracker(habits).habits())[0]
        assert habit.done_today is True
        assert habit.streak == 12


class TestWriting:
    async def test_adding_a_task_posts_it(self) -> None:
        handler = replying({"id": "t9", "title": "Call the bank"})

        applied = await tracker(handler).add_task(title="Call the bank", priority=Priority.HIGH)

        request = handler.seen[0]
        assert request.method == "POST"
        assert request.url.path == "/api/tasks"
        assert json.loads(request.content) == {"title": "Call the bank", "priority": "high"}
        assert applied.what == "Call the bank"

    async def test_completing_uses_sunnys_own_toggle(self) -> None:
        """Rather than reaching past it to set a field: the toggle is where
        Sunny keeps whatever bookkeeping completion involves — streaks,
        recurrence — and a write that skips it would quietly diverge."""
        handler = replying({"id": "t1", "title": "Тренировка"})

        applied = await tracker(handler).complete_task(task_id="t1")

        assert handler.seen[0].url.path == "/api/tasks/t1/toggle"
        assert applied.what == "Тренировка"

    async def test_rescheduling_patches_the_deadline(self) -> None:
        handler = replying({"id": "t1", "title": "Report"})
        when = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)

        await tracker(handler).reschedule_task(task_id="t1", deadline=when)

        request = handler.seen[0]
        assert request.method == "PATCH"
        assert json.loads(request.content) == {"deadline": when.isoformat()}

    async def test_priority_uses_the_same_patch(self) -> None:
        """Sunny's taskUpdateSchema is taskCreateSchema.partial(), so no new
        endpoint was needed for this and none was added."""
        handler = replying({"id": "t1", "title": "Report"})

        await tracker(handler).set_priority(task_id="t1", priority=Priority.URGENT)

        assert json.loads(handler.seen[0].content) == {"priority": "urgent"}

    async def test_the_reply_names_what_changed(self) -> None:
        """ "Done" is not an answer when the question was which task, and a
        misheard title that got this far has to be visible to the owner."""
        handler = replying({"id": "t1", "title": "Cancel the gym"})

        applied = await tracker(handler).complete_task(task_id="t1")

        assert "gym" in applied.what.lower()

    async def test_a_goal_can_be_added_with_a_target(self) -> None:
        handler = replying({"id": "g1", "title": "Run 10k"})

        await tracker(handler).add_goal(title="Run 10k", target_date=date(2026, 12, 31))

        assert json.loads(handler.seen[0].content)["targetDate"] == "2026-12-31"


class TestTheToken:
    async def test_it_is_sent_as_a_bearer_header(self) -> None:
        handler = replying([])

        await tracker(handler).tasks_today()

        assert handler.seen[0].headers["authorization"] == f"Bearer {TOKEN}"

    async def test_it_never_appears_in_the_url(self) -> None:
        """A query parameter would put it in Sunny's access logs, in Vercel's,
        and in every error message httpx composes."""
        handler = replying([])

        await tracker(handler).tasks_today()

        assert TOKEN not in str(handler.seen[0].url)

    async def test_a_transport_failure_does_not_quote_the_url_back(self) -> None:
        """httpx puts the URL in its messages, and the URL is where the token
        would be if anything ever moved it there. The type is enough."""

        async def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(TrackerError) as caught:
            await tracker(broken).tasks_today()

        assert TOKEN not in str(caught.value)
        assert "sunny.example" not in str(caught.value)


class TestWhenSunnyRefuses:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_rejected_token_says_so_plainly(self, status: int) -> None:
        with pytest.raises(TrackerUnavailableError, match="refused the token"):
            await tracker(replying({}, status)).tasks_today()

    async def test_a_server_error_is_reported_with_its_code(self) -> None:
        with pytest.raises(TrackerError, match="500"):
            await tracker(replying({}, 500)).tasks_today()

    async def test_a_timeout_is_its_own_message(self) -> None:
        async def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(TrackerError, match="did not answer in time"):
            await tracker(slow).tasks_today()

    async def test_a_non_json_answer_is_not_a_crash(self) -> None:
        async def html(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<!doctype html><title>Vercel</title>")

        with pytest.raises(TrackerError, match="not JSON"):
            await tracker(html).tasks_today()
