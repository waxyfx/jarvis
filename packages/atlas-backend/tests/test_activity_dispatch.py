"""Answering "сколько я сегодня работал?" from rows the agent actually sent.

The arithmetic is tested next door, without a database. What is tested here is
the join: samples written for *this* device are read back for *this* device,
through the same dispatch path as everything else, with no agent connected.

That last part is the point. The laptop may be shut; the question is still
answerable, because the backend is where the day was recorded.
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
from tests.conftest import authenticate, pair_device, requires_db, run_sql

pytestmark = [requires_db, pytest.mark.integration]


def record(
    device_id: str,
    *,
    minutes: float,
    app: str = "Code.exe",
    idle: bool = False,
    ending: datetime | None = None,
) -> None:
    """Write samples the way the agent's monitor would have, ten seconds apart."""
    finish = ending or datetime.now(UTC)
    start = finish - timedelta(minutes=minutes)
    for index in range(int(minutes * 6) + 1):
        run_sql(
            "INSERT INTO activity_samples (device_id, ts, process_name, is_idle, idle_seconds) "
            "VALUES (:device_id, :ts, :process_name, :is_idle, :idle_seconds)",
            device_id=device_id,
            ts=start + timedelta(seconds=index * 10),
            process_name=app,
            is_idle=idle,
            idle_seconds=300 if idle else 0,
        )


@contextmanager
def assistant(settings, script: Sequence[object]) -> Iterator[tuple[TestClient, str, str]]:  # type: ignore[no-untyped-def]
    app = create_app(settings, ai_provider=ScriptedProvider(list(script)))
    with TestClient(app) as client:
        device = pair_device(client)
        yield client, authenticate(client, device), device.device_id


def say(client: TestClient, token: str, text: str) -> dict[str, Any]:
    response = client.post(
        "/v1/assistant/message",
        json={"text": text, "language": "ru"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestAnsweringFromTheRecord:
    def test_it_answers_with_no_agent_connected(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The proof it ran on the backend: an agent tool in this suite comes
        back `unreachable`, because there is no agent. The laptop being shut is
        exactly when this question gets asked."""
        script = [tool_reply(("activity.today", {})), text_reply("Около часа, сэр.")]

        with assistant(settings, script) as (client, token, device_id):
            record(device_id, minutes=60)
            answer = say(client, token, "сколько я сегодня работал")

        call = answer["executed"][0]
        assert call["status"] == "completed"
        assert 58 <= call["result"]["active_minutes"] <= 62

    def test_it_says_what_the_time_went_on(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [tool_reply(("activity.today", {})), text_reply("В основном VS Code.")]

        with assistant(settings, script) as (client, token, device_id):
            now = datetime.now(UTC)
            record(device_id, minutes=40, ending=now - timedelta(minutes=20))
            record(device_id, minutes=20, app="chrome.exe", ending=now)
            answer = say(client, token, "чем я сегодня занимался")

        apps = [app["name"] for app in answer["executed"][0]["result"]["apps"]]
        assert apps[0] == "VS Code"
        assert "Chrome" in apps

    def test_another_devices_day_is_not_this_devices_day(self, settings) -> None:  # type: ignore[no-untyped-def]
        """One backend, more than one machine. A phone asking what the laptop
        did is a different question from the laptop's own."""
        script = [tool_reply(("activity.today", {})), text_reply("Ничего, сэр.")]

        with assistant(settings, script) as (client, token, _):
            record(str(uuid.uuid4()), minutes=90)
            answer = say(client, token, "сколько я работал")

        assert answer["executed"][0]["result"]["active_minutes"] == 0

    def test_a_day_with_nothing_recorded_is_zero_not_a_failure(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The agent may never have run today. "No time recorded" is an answer;
        an error is not."""
        script = [tool_reply(("activity.today", {})), text_reply("Ничего не записано, сэр.")]

        with assistant(settings, script) as (client, token, _):
            answer = say(client, token, "сколько я работал")

        assert answer["executed"][0]["status"] == "completed"
        assert answer["executed"][0]["result"]["active_minutes"] == 0

    def test_a_rolling_window_asks_only_about_that_window(self, settings) -> None:  # type: ignore[no-untyped-def]
        """ "How long have I been at this?" is a different question from "how
        long today", and the model has an argument for it."""
        script = [tool_reply(("activity.today", {"hours": 1})), text_reply("Час, сэр.")]

        with assistant(settings, script) as (client, token, device_id):
            now = datetime.now(UTC)
            record(device_id, minutes=120, ending=now - timedelta(hours=3))
            record(device_id, minutes=30, ending=now)
            answer = say(client, token, "сколько я сижу за компьютером")

        result = answer["executed"][0]["result"]
        assert result["window_hours"] == 1
        assert 28 <= result["active_minutes"] <= 32, "the earlier session is outside the window"


class TestTheSurface:
    def test_it_is_offered_even_with_no_tracker_and_no_web(self, settings) -> None:  # type: ignore[no-untyped-def]
        """It depends on nothing but the database, which is always there."""
        provider = ScriptedProvider([text_reply("Привет.")])
        app = create_app(
            settings.model_copy(update={"web_tools_enabled": False}),
            ai_provider=provider,
            tracker=None,
        )

        with TestClient(app) as client:
            say(client, authenticate(client, pair_device(client)), "привет")

        assert "activity.today" in {tool.name for tool in provider.requests[0].tools}

    def test_an_invented_activity_tool_is_refused_before_anything_runs(self, settings) -> None:  # type: ignore[no-untyped-def]
        script = [tool_reply(("activity.keystrokes", {})), text_reply("Не могу, сэр.")]

        with assistant(settings, script) as (client, token, _):
            answer = say(client, token, "что я печатал")

        assert answer["executed"] == []
        assert answer["rejected"][0]["tool"] == "activity.keystrokes"
