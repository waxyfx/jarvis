"""«Напомни мне через двадцать минут» — set, said once, and not twice.

The delivery half is where the interesting failures are. A reminder that never
arrives is a broken promise; one that arrives eleven times is worse, because the
owner switches the whole thing off. Both are tested here, and so is the case
that decides which failure you get: marking delivered in the same transaction
that reads it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from atlas_backend.ai import ScriptedProvider, text_reply, tool_reply
from atlas_backend.main import create_app
from atlas_shared.enums import NotificationKind
from tests.conftest import authenticate, fetch_sql, pair_device, requires_db

pytestmark = [requires_db, pytest.mark.integration]


@contextmanager
def assistant(settings, script: Sequence[object]) -> Iterator[tuple[TestClient, str, str]]:  # type: ignore[no-untyped-def]
    app = create_app(settings, ai_provider=ScriptedProvider(list(script)))
    with TestClient(app) as client:
        device = pair_device(client)
        yield client, authenticate(client, device), device.device_id


def _run_tool(client: TestClient, token: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool directly, without the model.

    Used where a step needs an id that only a previous step produced — the
    scripted provider is fixed in advance and cannot carry one forward.
    """
    response = client.post(
        f"/v1/tools/{tool}/execute",
        json={"args": args},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def say(client: TestClient, token: str, text: str) -> dict[str, Any]:
    response = client.post(
        "/v1/assistant/message",
        json={"text": text, "language": "ru"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestSettingOne:
    def test_a_delay_becomes_a_reminder(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("reminder.set", {"text": "позвонить маме", "in_minutes": 20})),
            text_reply("Напомню через двадцать минут, сэр."),
        ]

        with assistant(settings, script) as (client, token, _):
            answer = say(client, token, "напомни мне через 20 минут позвонить маме")

        result = answer["executed"][0]["result"]
        assert result["set"] == "позвонить маме"
        assert result["in_minutes"] == 20

    def test_an_absolute_time_works_too(self, settings) -> None:  # type: ignore[no-untyped-def]
        when = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
        script = [
            tool_reply(("reminder.set", {"text": "встреча", "at": when})),
            text_reply("Хорошо, сэр."),
        ]

        with assistant(settings, script) as (client, token, _):
            answer = say(client, token, "напомни в шесть")

        assert 175 <= answer["executed"][0]["result"]["in_minutes"] <= 181

    def test_a_time_that_has_passed_is_refused_rather_than_fired_at_once(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The model misreads an hour occasionally. Setting a reminder for the
        past would make it go off immediately, which reads as a bug."""
        gone = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        script = [
            tool_reply(("reminder.set", {"text": "что-то", "at": gone})),
            text_reply("Это время уже прошло, сэр."),
        ]

        with assistant(settings, script) as (client, token, _):
            answer = say(client, token, "напомни в час дня")

        assert answer["executed"][0]["result"] is None
        assert answer["stopped_because"] == "completed"

    def test_something_a_month_away_belongs_in_the_tracker(self, settings) -> None:  # type: ignore[no-untyped-def]
        far = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        script = [
            tool_reply(("reminder.set", {"text": "продлить паспорт", "at": far})),
            text_reply("Лучше задачей, сэр."),
        ]

        with assistant(settings, script) as (client, token, _):
            answer = say(client, token, "напомни через месяц")

        assert answer["executed"][0]["result"] is None

    def test_an_unparseable_time_is_refused_by_name(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("reminder.set", {"text": "что-то", "at": "завтра вечером"})),
            text_reply("Уточните время, сэр."),
        ]

        with assistant(settings, script) as (client, token, _):
            answer = say(client, token, "напомни завтра вечером")

        assert answer["executed"][0]["result"] is None


class TestListingAndCancelling:
    def test_what_is_waiting_can_be_read_back(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("reminder.set", {"text": "позвонить маме", "in_minutes": 20})),
            text_reply("Готово."),
            tool_reply(("reminder.list", {})),
            text_reply("Одно напоминание, сэр."),
        ]

        with assistant(settings, script) as (client, token, _):
            say(client, token, "напомни позвонить маме через 20 минут")
            answer = say(client, token, "какие у меня напоминания")

        result = answer["executed"][0]["result"]
        assert result["total"] == 1
        assert result["reminders"][0]["text"] == "позвонить маме"

    def test_cancelling_reports_which_one(self, settings) -> None:  # type: ignore[no-untyped-def]
        """ "Отменено" is not an answer when the question was which. A misheard
        id that got this far has to reach the owner's ears.

        The id has to come from `reminder.list` rather than from the test,
        because that is the only way the model can get one — it is told never to
        invent them.
        """
        script = [
            tool_reply(("reminder.set", {"text": "позвонить маме", "in_minutes": 20})),
            text_reply("Готово."),
            tool_reply(("reminder.list", {})),
            text_reply("Одно."),
        ]

        with assistant(settings, script) as (client, token, _):
            say(client, token, "напомни позвонить маме")
            listed = say(client, token, "какие напоминания")
            identifier = listed["executed"][0]["result"]["reminders"][0]["id"]

            cancelled = _run_tool(client, token, "reminder.cancel", {"reminder_id": identifier})

        assert cancelled["result"] == {"cancelled": "позвонить маме"}

    def test_an_invented_id_is_refused(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("reminder.cancel", {"reminder_id": str(uuid.uuid4())})),
            text_reply("Не нашёл, сэр."),
        ]

        with assistant(settings, script) as (client, token, _):
            answer = say(client, token, "отмени")

        assert answer["executed"][0]["result"] is None

    def test_a_cancelled_reminder_is_not_listed(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [
            tool_reply(("reminder.set", {"text": "что-то", "in_minutes": 30})),
            text_reply("Готово."),
            tool_reply(("reminder.list", {})),
            text_reply("Ничего."),
        ]

        with assistant(settings, script) as (client, token, _):
            created = say(client, token, "напомни")["executed"][0]["result"]["id"]
            _run_tool(client, token, "reminder.cancel", {"reminder_id": created})
            answer = say(client, token, "что осталось")

        assert answer["executed"][0]["result"]["total"] == 0


class TestSayingIt:
    """The half that decides whether reminders get trusted."""

    async def test_a_due_reminder_is_delivered_once(self, settings) -> None:  # type: ignore[no-untyped-def]
        from atlas_backend.ws.hub import Hub
        from tests.test_notify_scheduler import RecordingNotifier, connect, local_now, scheduler

        now = local_now(14)
        hub = Hub()
        device_id = connect(hub)
        await _insert(device_id, "позвонить маме", due=now - timedelta(minutes=1))
        notifier = RecordingNotifier()
        engine = scheduler(settings, hub, notifier)

        assert await engine.tick(now) == 1
        assert notifier.kinds == [NotificationKind.REMINDER]
        assert "позвонить маме" in notifier.sent[0][1].body

        # The second tick is the one that matters: a reminder said twice is
        # worse than one said late.
        assert await engine.tick(now + timedelta(minutes=1)) == 0

    async def test_it_is_said_even_with_nobody_at_the_machine(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Every other rule checks presence first. This one was asked for at a
        moment, and going quiet because the chair is empty is the failure that
        makes someone stop trusting reminders."""
        from atlas_backend.ws.hub import Hub
        from tests.test_notify_scheduler import RecordingNotifier, connect, local_now, scheduler

        now = local_now(14)
        hub = Hub()
        device_id = connect(hub)
        await _insert(device_id, "выключить плиту", due=now - timedelta(minutes=1))
        notifier = RecordingNotifier()

        # No activity samples at all, so presence is false.
        assert await scheduler(settings, hub, notifier).tick(now) == 1

    async def test_one_that_is_not_due_stays_quiet(self, settings) -> None:  # type: ignore[no-untyped-def]
        from atlas_backend.ws.hub import Hub
        from tests.test_notify_scheduler import RecordingNotifier, connect, local_now, scheduler

        now = local_now(14)
        hub = Hub()
        device_id = connect(hub)
        await _insert(device_id, "позже", due=now + timedelta(hours=2))
        notifier = RecordingNotifier()

        assert await scheduler(settings, hub, notifier).tick(now) == 0

    async def test_a_cancelled_one_is_never_said(self, settings) -> None:  # type: ignore[no-untyped-def]
        from atlas_backend.ws.hub import Hub
        from tests.test_notify_scheduler import RecordingNotifier, connect, local_now, scheduler

        now = local_now(14)
        hub = Hub()
        device_id = connect(hub)
        await _insert(device_id, "отменено", due=now - timedelta(minutes=1), cancelled=True)
        notifier = RecordingNotifier()

        assert await scheduler(settings, hub, notifier).tick(now) == 0

    async def test_a_backlog_is_rationed_rather_than_recited(self, settings) -> None:  # type: ignore[no-untyped-def]
        """A backend that was down for a day comes back to a pile. Saying forty
        things in a row is not a reminder, it is a reason to switch this off."""
        from atlas_backend.ws.hub import Hub
        from tests.test_notify_scheduler import RecordingNotifier, connect, local_now, scheduler

        now = local_now(14)
        hub = Hub()
        device_id = connect(hub)
        for index in range(10):
            await _insert(device_id, f"дело {index}", due=now - timedelta(minutes=index + 1))
        notifier = RecordingNotifier()

        assert await scheduler(settings, hub, notifier).tick(now) == 3


async def _insert(
    device_id: uuid.UUID, text: str, *, due: datetime, cancelled: bool = False
) -> None:
    from tests.conftest import insert_rows

    await insert_rows(
        "INSERT INTO reminders (id, user_id, device_id, text, due_at, created_at, cancelled_at) "
        "VALUES (:id, :user_id, :device_id, :text, :due, :created, :cancelled)",
        [
            {
                "id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()),
                "device_id": str(device_id),
                "text": text,
                "due": due,
                "created": datetime.now(UTC),
                "cancelled": datetime.now(UTC) if cancelled else None,
            }
        ],
    )


def test_the_audit_trail_records_the_tool_like_any_other(settings) -> None:  # type: ignore[no-untyped-def]
    script = [
        tool_reply(("reminder.set", {"text": "что-то", "in_minutes": 5})),
        text_reply("Готово."),
    ]

    with assistant(settings, script) as (client, token, _):
        say(client, token, "напомни")

    events = [row[0] for row in fetch_sql("SELECT event_type FROM audit_log ORDER BY seq")]
    assert "tool.executed" in events
