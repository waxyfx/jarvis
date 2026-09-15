"""Delivering a notification on the machine where the person is.

The backend decided it was worth saying; this is the last chance to get the
*how* wrong. Three ways to get it wrong, and all three are here: saying it
twice, saying it aloud when the owner has hit the kill switch, and losing it
entirely because one half of the delivery failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas_agent.notifications import NotificationDelivery
from atlas_agent.safety.mode import ModeChangeSource, SafeModeController
from atlas_shared.enums import Language, NotificationKind, NotificationPriority
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.messages import Notify


def notification(
    *,
    priority: NotificationPriority = NotificationPriority.NORMAL,
    speak: bool = True,
    notification_id: str | None = None,
) -> Notify:
    return Notify(
        notification_id=notification_id or new_ulid(),
        kind=NotificationKind.REMINDER,
        priority=priority,
        title="Тренировка",
        body="Через 15 мин — тренировка, сэр.",
        speak=speak,
    )


class Spoken:
    """Records what was said out loud, and in what language."""

    def __init__(self, *, broken: bool = False) -> None:
        self.said: list[str] = []
        self._broken = broken

    async def __call__(self, text: str, language: Language) -> None:
        if self._broken:
            raise RuntimeError("the speakers are busy")
        self.said.append(text)


class Shown:
    def __init__(self, *, broken: bool = False) -> None:
        self.shown: list[tuple[str, str]] = []
        self._broken = broken

    def __call__(self, title: str, body: str) -> None:
        if self._broken:
            raise RuntimeError("no tray icon")
        self.shown.append((title, body))


class TestDelivering:
    async def test_it_is_said_and_shown(self) -> None:
        speak, show = Spoken(), Shown()

        delivered = await NotificationDelivery(speak=speak, show=show).deliver(notification())

        assert delivered is True
        assert speak.said == ["Через 15 мин — тренировка, сэр."]
        assert show.shown == [("Тренировка", "Через 15 мин — тренировка, сэр.")]

    async def test_the_same_one_twice_is_said_once(self) -> None:
        """A reconnect can redeliver. The same sentence twice in one minute is
        worse than not saying it at all."""
        speak = Spoken()
        delivery = NotificationDelivery(speak=speak)
        same = notification(notification_id="01ABC")

        assert await delivery.deliver(same) is True
        assert await delivery.deliver(same) is False
        assert len(speak.said) == 1

    async def test_a_low_priority_notification_is_shown_and_not_spoken(self) -> None:
        speak, show = Spoken(), Shown()

        await NotificationDelivery(speak=speak, show=show).deliver(
            notification(priority=NotificationPriority.LOW)
        )

        assert speak.said == []
        assert len(show.shown) == 1

    async def test_speak_false_is_honoured_whatever_the_priority(self) -> None:
        speak = Spoken()

        await NotificationDelivery(speak=speak, show=Shown()).deliver(notification(speak=False))

        assert speak.said == []


class TestWhenSomethingIsWrong:
    async def test_a_failed_tray_does_not_swallow_the_spoken_half(self) -> None:
        """The tray icon is a nicety; the voice is what was asked for."""
        speak = Spoken()

        delivered = await NotificationDelivery(speak=speak, show=Shown(broken=True)).deliver(
            notification()
        )

        assert delivered is True
        assert len(speak.said) == 1

    async def test_a_failed_voice_still_leaves_something_to_look_at(self) -> None:
        show = Shown()

        delivered = await NotificationDelivery(speak=Spoken(broken=True), show=show).deliver(
            notification()
        )

        assert delivered is True
        assert len(show.shown) == 1

    async def test_with_nowhere_to_deliver_it_reports_failure_rather_than_raising(self) -> None:
        """A headless agent. The backend has already recorded that it decided to
        speak; this must not become a crash in the message loop."""
        assert await NotificationDelivery().deliver(notification()) is False


class TestSafeMode:
    async def test_the_kill_switch_silences_the_voice(self, tmp_path: Path) -> None:
        """A voice in the room is the most intrusive thing this system does, and
        SAFE MODE means stop acting."""
        controller = SafeModeController(tmp_path / "mode.json")
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_TRAY)
        speak, show = Spoken(), Shown()

        await NotificationDelivery(speak=speak, show=show, safe_mode=controller).deliver(
            notification()
        )

        assert speak.said == []

    async def test_but_the_notification_is_still_there_to_read(self, tmp_path: Path) -> None:
        """Silenced, not suppressed. Losing a reminder outright loses
        information the owner may well want."""
        controller = SafeModeController(tmp_path / "mode.json")
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_TRAY)
        show = Shown()

        delivered = await NotificationDelivery(
            speak=Spoken(), show=show, safe_mode=controller
        ).deliver(notification())

        assert delivered is True
        assert len(show.shown) == 1

    async def test_normal_mode_speaks(self, tmp_path: Path) -> None:
        """The control: without it the test above would pass with the voice
        broken for an entirely different reason."""
        speak = Spoken()

        await NotificationDelivery(
            speak=speak, safe_mode=SafeModeController(tmp_path / "mode.json")
        ).deliver(notification())

        assert len(speak.said) == 1


class TestBoundedMemory:
    async def test_remembering_ids_does_not_grow_without_limit(self) -> None:
        """This process runs for weeks."""
        delivery = NotificationDelivery(speak=Spoken())

        for _ in range(1000):
            await delivery.deliver(notification())

        assert len(delivery._delivered) <= 256


@pytest.mark.parametrize("language", [Language.RU, Language.EN])
async def test_it_speaks_in_the_configured_language(language: Language) -> None:
    said: list[Language] = []

    async def speak(text: str, spoken_in: Language) -> None:
        said.append(spoken_in)

    await NotificationDelivery(speak=speak, language=language).deliver(notification())

    assert said == [language]
