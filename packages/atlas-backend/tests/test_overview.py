"""The one request a phone makes to draw its screen.

A phone foregrounds on a train, and whatever it needs it needs in one round
trip. So this endpoint gathers what five separate calls would have returned —
and, more importantly, gathers it from rows already written rather than by
reaching out to the laptop, so the answer is the same whether the machine is
awake or shut in a bag.

The assertions that matter are about honesty rather than completeness. Telemetry
from an hour ago is still shown, with its age, because hiding it loses the only
evidence of when the machine was last doing anything. And a day with no samples
answers `null` rather than `0`: one is a claim about the record, the other is a
claim about the day, and only the first is true when the agent was not running.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from atlas_backend.ai import ScriptedProvider, text_reply, tool_reply
from atlas_backend.main import create_app
from tests.conftest import authenticate, insert_rows, pair_device, requires_db, run_sql

pytestmark = [requires_db, pytest.mark.integration]


def get_overview(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get("/v1/overview", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return dict(response.json())


def record_telemetry(device_id: str, *, ago: timedelta, cpu: float = 12.5) -> None:
    run_sql(
        "INSERT INTO system_telemetry (device_id, ts, cpu_pct, ram_used_pct, ram_total_mb, "
        "disks, uptime_s) VALUES (:device_id, :ts, :cpu, :ram, :total, '[]'::jsonb, :uptime)",
        device_id=device_id,
        ts=datetime.now(UTC) - ago,
        cpu=cpu,
        ram=61.0,
        total=15000,
        uptime=7200,
    )


class TestTheMachine:
    def test_it_reports_the_paired_windows_machine(self, settings) -> None:  # type: ignore[no-untyped-def]
        with TestClient(create_app(settings)) as client:
            device = pair_device(client)
            answer = get_overview(client, authenticate(client, device))

        assert len(answer["machines"]) == 1
        assert answer["machines"][0]["device_id"] == device.device_id

    def test_a_machine_with_no_open_socket_is_not_connected(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The one field here that is certainly current. Everything else is a
        memory of what the agent last said."""
        with TestClient(create_app(settings)) as client:
            device = pair_device(client)
            answer = get_overview(client, authenticate(client, device))

        assert answer["machines"][0]["connected"] is False

    def test_recent_telemetry_is_reported_as_current(self, settings) -> None:  # type: ignore[no-untyped-def]
        with TestClient(create_app(settings)) as client:
            device = pair_device(client)
            record_telemetry(device.device_id, ago=timedelta(seconds=30))
            answer = get_overview(client, authenticate(client, device))

        machine = answer["machines"][0]
        assert machine["cpu_pct"] == 12.5
        assert machine["telemetry_stale"] is False
        assert machine["telemetry_age_s"] < 120

    def test_old_telemetry_is_shown_with_its_age_rather_than_hidden(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Suppressing it would leave the screen blank, which reads as "nothing
        is known" when what is actually known is "it was at 12% an hour ago and
        has said nothing since"."""
        with TestClient(create_app(settings)) as client:
            device = pair_device(client)
            record_telemetry(device.device_id, ago=timedelta(hours=1))
            answer = get_overview(client, authenticate(client, device))

        machine = answer["machines"][0]
        assert machine["cpu_pct"] == 12.5
        assert machine["telemetry_stale"] is True
        assert machine["telemetry_age_s"] > 3000

    def test_a_machine_that_never_reported_has_no_numbers_at_all(self, settings) -> None:  # type: ignore[no-untyped-def]
        with TestClient(create_app(settings)) as client:
            device = pair_device(client)
            answer = get_overview(client, authenticate(client, device))

        assert answer["machines"][0]["cpu_pct"] is None
        assert answer["machines"][0]["telemetry_age_s"] is None


class TestWhatIsWaiting:
    def test_an_action_held_for_confirmation_appears(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Most of why this endpoint exists: the phone is a good place to
        answer a confirmation the laptop asked for."""
        script = [tool_reply(("app.close", {"name": "chrome"})), text_reply("Подтвердите.")]
        app = create_app(settings, ai_provider=ScriptedProvider(script))

        with TestClient(app) as client:
            device = pair_device(client)
            token = authenticate(client, device)
            said = client.post(
                "/v1/assistant/message",
                json={"text": "закрой chrome", "language": "ru"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert said.status_code == 200, said.text
            answer = get_overview(client, token)

        assert len(answer["pending"]) == 1
        assert answer["pending"][0]["tool"] == "app.close"
        assert answer["pending"][0]["risk"] in {"medium", "high"}

    def test_nothing_waiting_is_an_empty_list(self, settings) -> None:  # type: ignore[no-untyped-def]
        with TestClient(create_app(settings)) as client:
            device = pair_device(client)
            answer = get_overview(client, authenticate(client, device))

        assert answer["pending"] == []


class TestTheDay:
    async def test_it_reports_minutes_at_the_machine(self, settings) -> None:  # type: ignore[no-untyped-def]
        app = create_app(settings)
        with TestClient(app) as client:
            device = pair_device(client)
            token = authenticate(client, device)
            await _record_activity(device.device_id, minutes=40)
            answer = get_overview(client, token)

        assert 38 <= answer["active_minutes_today"] <= 42

    def test_a_day_with_nothing_recorded_answers_null_not_zero(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Zero is a claim about the day. Null is a claim about the record, and
        it is the true one when the agent was never running."""
        with TestClient(create_app(settings)) as client:
            device = pair_device(client)
            answer = get_overview(client, authenticate(client, device))

        assert answer["active_minutes_today"] is None


class TestItStaysARead:
    def test_it_answers_without_the_laptop_being_there(self, settings) -> None:  # type: ignore[no-untyped-def]
        """No agent is connected in this suite, so an endpoint that reached for
        one would hang or fail. This one is rows only."""
        with TestClient(create_app(settings)) as client:
            device = pair_device(client)
            answer = get_overview(client, authenticate(client, device))

        assert answer["at"]
        assert answer["machines"][0]["connected"] is False

    def test_it_needs_a_paired_device(self, settings) -> None:  # type: ignore[no-untyped-def]
        with TestClient(create_app(settings)) as client:
            assert client.get("/v1/overview").status_code == 401


async def _record_activity(device_id: str, *, minutes: float) -> None:
    now = datetime.now(UTC)
    start = now - timedelta(minutes=minutes)
    await insert_rows(
        "INSERT INTO activity_samples (device_id, ts, process_name, is_idle, idle_seconds) "
        "VALUES (:device_id, :ts, :process_name, :is_idle, 0)",
        [
            {
                "device_id": device_id,
                "ts": start + timedelta(seconds=index * 10),
                "process_name": "Code.exe",
                "is_idle": False,
            }
            for index in range(int(minutes * 6) + 1)
        ],
    )
