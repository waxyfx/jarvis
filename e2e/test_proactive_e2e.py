"""A notification from the backend's clock to the owner's speakers.

Everything else about proactive notifications is tested in pieces: the rules
against a made-up Tuesday, the scheduler against a database, the delivery
against fakes. This is the piece none of those can cover — that the signed
message survives the wire.

A real backend on a real socket, a real agent connected to it with its own
device key and the server key pinned at pairing. The notification is built by
the backend, signed with the server's key, sent over the websocket, verified by
the agent, and delivered. If the signature were wrong the agent would not merely
drop it — it would enter SAFE MODE, which is asserted below as the other half
of the same behaviour.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from atlas_backend.notify.notifier import Notifier
from atlas_shared.enums import AgentMode, NotificationKind, NotificationPriority
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.messages import Notify, build_envelope
from e2e.conftest import E2E_BOOTSTRAP_TOKEN, backend_settings, requires_e2e_db
from e2e.harness import start_stack

pytestmark = [requires_e2e_db, pytest.mark.integration]


class Recorder:
    """Stands in for the speakers, which a test machine may not have."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.shown: list[tuple[str, str]] = []

    async def speak(self, text: str, language: Any) -> None:
        self.said.append(text)

    def show(self, title: str, body: str) -> None:
        self.shown.append((title, body))


def reminder(**kwargs: Any) -> Notify:
    return Notify(
        notification_id=kwargs.pop("notification_id", new_ulid()),
        kind=kwargs.pop("kind", NotificationKind.REMINDER),
        priority=kwargs.pop("priority", NotificationPriority.NORMAL),
        title=kwargs.pop("title", "Тренировка"),
        body=kwargs.pop("body", "Через 15 мин — тренировка, сэр."),
        **kwargs,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """No file tools are used here; the stack just wants somewhere to look."""
    (tmp_path / "allowed").mkdir()
    return tmp_path


@pytest.fixture
async def stack(tmp_path: Path, workspace: Path):  # type: ignore[no-untyped-def]
    from atlas_backend.ai import ScriptedProvider, text_reply

    running = await start_stack(
        provider=ScriptedProvider([text_reply("Готово.")]),
        settings_factory=backend_settings,
        tmp_path=tmp_path,
        workspace=workspace,
        allowed_roots=(),
        bootstrap_token=E2E_BOOTSTRAP_TOKEN,
        device_name="proactive-agent",
    )
    try:
        yield running
    finally:
        await running.shutdown()


def notifier_for(stack) -> tuple[Notifier, uuid.UUID]:  # type: ignore[no-untyped-def]
    """The backend's own notifier, and the device the agent is connected as."""
    hub = stack.app.state.hub
    connections = hub.snapshot()
    assert connections, "the agent should be connected"
    return (
        Notifier(
            hub=hub,
            server_identity=stack.app.state.server_identity,
            database=stack.app.state.database,
        ),
        connections[0].device_id,
    )


async def settle(delivered: list[str], *, timeout_s: float = 5.0) -> None:
    """Wait for the agent's side to catch up.

    The send returns as soon as the frame is written; the agent reads it on its
    own task. Polling rather than sleeping keeps the test quick when it passes
    and honest when it does not.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not delivered and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)


class TestTheChannel:
    async def test_a_notification_crosses_the_wire_and_is_delivered(self, stack) -> None:  # type: ignore[no-untyped-def]
        recorder = Recorder()
        stack.delivery.bind_voice(recorder.speak)
        stack.delivery.bind_display(recorder.show)
        notifier, device_id = notifier_for(stack)

        assert await notifier.send(device_id, reminder()) is True
        await settle(recorder.said)

        assert recorder.said == ["Через 15 мин — тренировка, сэр."]
        assert recorder.shown == [("Тренировка", "Через 15 мин — тренировка, сэр.")]

    async def test_a_low_priority_one_arrives_without_being_spoken(self, stack) -> None:  # type: ignore[no-untyped-def]
        recorder = Recorder()
        stack.delivery.bind_voice(recorder.speak)
        stack.delivery.bind_display(recorder.show)
        notifier, device_id = notifier_for(stack)

        await notifier.send(
            device_id,
            reminder(
                kind=NotificationKind.SUMMARY,
                priority=NotificationPriority.LOW,
                title="Итоги дня",
                body="За компьютером сегодня 3 ч 20 мин.",
                speak=False,
            ),
        )
        await settle(recorder.shown)  # type: ignore[arg-type]

        assert recorder.shown, "it should still have arrived"
        assert recorder.said == []

    async def test_sending_to_a_machine_that_is_not_there_is_reported_not_raised(
        self,
        stack,  # type: ignore[no-untyped-def]
    ) -> None:
        """The normal state of a laptop. A scheduler that had to catch an
        exception for it would grow a bare except, and then a real failure would
        look the same as a closed lid."""
        notifier, _ = notifier_for(stack)

        assert await notifier.send(uuid.uuid4(), reminder()) is False


class TestTheSignature:
    async def test_an_unsigned_notification_is_refused_and_trips_safe_mode(self, stack) -> None:  # type: ignore[no-untyped-def]
        """The reason this message is signed at all. Without verification, the
        websocket would be a channel for saying anything in the assistant's
        voice, in the owner's room, while they are not looking at the screen.

        SAFE MODE on failure is the existing behaviour for any command that does
        not verify, and it applies here too: a frame that claims to be from the
        backend and is not means something is wrong with more than one message.
        """
        recorder = Recorder()
        stack.delivery.bind_voice(recorder.speak)
        hub = stack.app.state.hub
        device_id = hub.snapshot()[0].device_id

        # Built and sent exactly as the notifier does, minus the signing step.
        forged = build_envelope(
            "server.notify", reminder(body="Позвоните в банк."), corr_id=new_ulid()
        )
        await hub.send(device_id, forged.to_json())

        await asyncio.sleep(1.0)

        assert recorder.said == [], "an unsigned notification must not be spoken"
        assert stack.session.controller.mode is AgentMode.SAFE
