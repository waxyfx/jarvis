"""Where a tracker call runs, proved rather than asserted.

The claim is that tracker tools execute on the backend and never become a signed
command to the agent. Proving it directly would mean inspecting what did not
happen, which is awkward; this suite has a better instrument. **No agent is
connected here.** Anything dispatched to one comes back `unreachable` — the
existing tests rely on that. So a tracker call that *completes* completed
somewhere else, and there is only one somewhere else.

The rest is the property that matters more than where it ran: policy applies
identically either way. Risk is assessed, MEDIUM is held for confirmation, and
the audit trail records it, before anything reaches Sunny.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any

import pytest
from starlette.testclient import TestClient

from atlas_backend.ai import ScriptedProvider, text_reply, tool_reply
from atlas_backend.main import create_app
from atlas_backend.tracker.provider import Applied, Goal, Habit, Priority, Task, TrackerError
from tests.conftest import authenticate, fetch_sql, pair_device, requires_db

pytestmark = [requires_db, pytest.mark.integration]


class FakeTracker:
    """A tracker that answers instantly and remembers being asked."""

    name = "fake"

    def __init__(self, *, tasks: list[Task] | None = None, broken: bool = False) -> None:
        self._tasks = tasks or []
        self._broken = broken
        self.completed: list[str] = []

    async def tasks_today(self) -> list[Task]:
        if self._broken:
            raise TrackerError("the tracker is not answering")
        return self._tasks

    async def tasks_upcoming(self, *, days: int = 7) -> list[Task]:
        return self._tasks

    async def goals(self) -> list[Goal]:
        return []

    async def habits(self) -> list[Habit]:
        return []

    async def add_task(
        self,
        *,
        title: str,
        priority: Priority = Priority.MEDIUM,
        deadline: datetime | None = None,
    ) -> Applied:
        return Applied(what=title, detail="added")

    async def add_goal(self, *, title: str, target_date: date | None = None) -> Applied:
        return Applied(what=title, detail="added")

    async def complete_task(self, *, task_id: str) -> Applied:
        self.completed.append(task_id)
        return Applied(what="Тренировка", detail="completed")

    async def reschedule_task(self, *, task_id: str, deadline: datetime) -> Applied:
        return Applied(what="Отчёт", detail="moved")

    async def set_priority(self, *, task_id: str, priority: Priority) -> Applied:
        return Applied(what="Отчёт", detail="priority")


def a_task(title: str, at: str | None = None) -> Task:
    return Task(id=title.lower(), title=title, deadline=datetime.now(UTC), at=at)


@contextmanager
def assistant(
    settings, script: Sequence[object], tracker: Any = None
) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    app = create_app(settings, ai_provider=ScriptedProvider(list(script)), tracker=tracker)
    with TestClient(app) as client:
        device = pair_device(client)
        yield client, authenticate(client, device)


def say(client: TestClient, token: str, text: str) -> dict[str, Any]:
    response = client.post(
        "/v1/assistant/message",
        json={"text": text, "language": "ru"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def audit_events() -> list[str]:
    return [row[0] for row in fetch_sql("SELECT event_type FROM audit_log ORDER BY seq")]


class TestItRunsHere:
    def test_a_tracker_call_completes_with_no_agent_connected(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The proof. An agent tool in this suite comes back `unreachable`
        because there is no agent; this one completes."""
        tracker = FakeTracker(tasks=[a_task("Тренировка", "15:00")])
        script = [tool_reply(("tracker.today", {})), text_reply("Одна задача, сэр.")]

        with assistant(settings, script, tracker) as (client, token):
            answer = say(client, token, "что у меня сегодня")

        call = answer["executed"][0]
        assert call["tool"] == "tracker.today"
        assert call["status"] == "completed"
        assert call["result"]["total"] == 1

    def test_an_agent_tool_in_the_same_suite_still_cannot_complete(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The control. Without it the test above proves only that something
        happened, not that the two paths differ."""
        script = [tool_reply(("system.metrics", {})), text_reply("Готово.")]

        with assistant(settings, script, FakeTracker()) as (client, token):
            answer = say(client, token, "покажи память")

        assert answer["executed"][0]["status"] == "unreachable"

    def test_the_model_is_given_a_digest_rather_than_a_list(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The whole reason the tracker summarises: whatever is in the result is
        what the model has read, and may recite."""
        tracker = FakeTracker(tasks=[a_task(f"Task {index}") for index in range(11)])
        script = [tool_reply(("tracker.today", {})), text_reply("Одиннадцать задач.")]

        with assistant(settings, script, tracker) as (client, token):
            answer = say(client, token, "что у меня сегодня")

        result = answer["executed"][0]["result"]
        assert result["total"] == 11
        assert "items" not in result


class TestPolicyAppliesTheSame:
    def test_a_medium_tracker_action_is_held_for_confirmation(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Completing a task changes what the owner tracks by, and a misheard
        word doing that silently is the harm. The Policy Engine holds it exactly
        as it holds an agent tool."""
        tracker = FakeTracker()
        script = [
            tool_reply(("tracker.complete_task", {"task_id": "t1"})),
            text_reply("Подтвердите, сэр."),
        ]

        with assistant(settings, script, tracker) as (client, token):
            answer = say(client, token, "отметь тренировку выполненной")

        assert answer["executed"] == []
        assert answer["pending_confirmation"][0]["tool"] == "tracker.complete_task"
        assert tracker.completed == [], "it must not have run before being confirmed"

    def test_a_low_tracker_action_is_not_held(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("tracker.add_task", {"title": "Позвонить в банк"})),
            text_reply("Добавил."),
        ]

        with assistant(settings, script, FakeTracker()) as (client, token):
            answer = say(client, token, "добавь задачу")

        assert answer["executed"][0]["result"] == {"added": "Позвонить в банк"}

    def test_it_is_audited_like_anything_else(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [tool_reply(("tracker.today", {})), text_reply("Готово.")]

        with assistant(settings, script, FakeTracker()) as (client, token):
            say(client, token, "что сегодня")

        events = audit_events()
        assert "tool.dispatched" in events
        assert "tool.executed" in events

    def test_an_invented_tracker_tool_is_refused_before_anything_runs(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("tracker.delete_everything", {"task_id": "t1"})),
            text_reply("Не могу, сэр."),
        ]

        with assistant(settings, script, FakeTracker()) as (client, token):
            answer = say(client, token, "удали всё")

        assert answer["executed"] == []
        assert answer["rejected"][0]["tool"] == "tracker.delete_everything"


class TestWhenTheTrackerIsNotThere:
    def test_its_tools_are_not_offered_at_all(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Offering a tool with nothing behind it earns a refusal the model can
        do nothing about, and teaches it to keep trying."""
        provider = ScriptedProvider([text_reply("Привет.")])
        app = create_app(settings, ai_provider=provider, tracker=None)

        with TestClient(app) as client:
            token = authenticate(client, pair_device(client))
            say(client, token, "привет")

        offered = {tool.name for tool in provider.requests[0].tools}
        assert not any(name.startswith("tracker.") for name in offered)
        assert "system.metrics" in offered

    def test_a_tracker_that_fails_is_reported_not_raised(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The assistant should say the tracker could not answer, in the same
        breath as everything else that turn — not fail the whole request."""
        script = [tool_reply(("tracker.today", {})), text_reply("Трекер недоступен, сэр.")]

        with assistant(settings, script, FakeTracker(broken=True)) as (client, token):
            answer = say(client, token, "что сегодня")

        assert answer["stopped_because"] == "completed"
        assert answer["executed"][0]["result"] is None
