"""The clock that gives the rules a chance to speak.

The rules are tested next door against a made-up Tuesday. What is tested here is
everything around them: which devices get asked about, where the activity
numbers come from, that a tracker being down does not take the wellness warning
with it, and that one tick's decisions are not re-made on the next.

Driven by calling ``tick`` directly rather than by waiting for the loop. A test
that sleeps for sixty seconds is a test that gets deleted.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from atlas_backend.notify.rules import Schedule
from atlas_backend.notify.scheduler import ProactiveScheduler
from atlas_backend.tracker.provider import Goal, Habit, Priority, Task, TrackerError
from atlas_backend.ws.hub import Connection, Hub
from atlas_shared.enums import DeviceKind, NotificationKind
from atlas_shared.protocol.messages import Notify
from tests.conftest import insert_rows, requires_db

pytestmark = [requires_db, pytest.mark.integration]

#: Mid-morning in Almaty, which is the zone the settings default to. Chosen so
#: the briefing window is open: the scheduler converts to local time, and a
#: test written in UTC would quietly land at four in the morning.
MORNING_LOCAL_HOUR = 9


def local_now(hour: int = MORNING_LOCAL_HOUR) -> datetime:
    """A moment that is ``hour`` o'clock where the owner lives."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Asia/Almaty")
    today = datetime.now(zone).date()
    return datetime(today.year, today.month, today.day, hour, 0, tzinfo=zone).astimezone(UTC)


class RecordingNotifier:
    def __init__(self, *, delivers: bool = True) -> None:
        self.sent: list[tuple[uuid.UUID, Notify]] = []
        self._delivers = delivers

    async def send(self, device_id: uuid.UUID, notification: Notify) -> bool:
        self.sent.append((device_id, notification))
        return self._delivers

    @property
    def kinds(self) -> list[NotificationKind]:
        return [notification.kind for _, notification in self.sent]


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
        return []


class StubSocket:
    """Enough of a websocket for the hub to hold a connection open."""

    async def send_text(self, payload: str) -> None:
        return None


def connect(hub: Hub, *, kind: DeviceKind = DeviceKind.WINDOWS_AGENT) -> uuid.UUID:
    device_id = uuid.uuid4()
    hub._connections[device_id] = Connection(
        device_id=device_id,
        device_kind=kind.value,
        session_id=uuid.uuid4(),
        websocket=StubSocket(),  # type: ignore[arg-type]
    )
    return device_id


async def record(
    device_id: uuid.UUID, *, minutes: float, until: datetime, idle: bool = False
) -> None:
    """Samples the way the agent's monitor would have sent them."""
    start = until - timedelta(minutes=minutes)
    await insert_rows(
        "INSERT INTO activity_samples (device_id, ts, process_name, is_idle, idle_seconds) "
        "VALUES (:device_id, :ts, :process_name, :is_idle, :idle_seconds)",
        [
            {
                "device_id": str(device_id),
                "ts": start + timedelta(seconds=index * 10),
                "process_name": "Code.exe",
                "is_idle": idle,
                "idle_seconds": 300 if idle else 0,
            }
            for index in range(int(minutes * 6) + 1)
        ],
    )


def scheduler(settings, hub: Hub, notifier: RecordingNotifier, **kwargs) -> ProactiveScheduler:  # type: ignore[no-untyped-def]
    from atlas_backend.db.session import Database

    return ProactiveScheduler(
        hub=hub,
        notifier=notifier,  # type: ignore[arg-type]
        database=Database(settings),
        settings=settings,
        **kwargs,
    )


class TestWhoItSpeaksTo:
    async def test_nothing_happens_with_no_agent_connected(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Most of the day. The laptop is shut and this loop is a no-op."""
        notifier = RecordingNotifier()

        assert await scheduler(settings, Hub(), notifier).tick(local_now()) == 0
        assert notifier.sent == []

    async def test_a_phone_is_not_spoken_to_this_way(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The phone has its own notifications. Saying it in both places is
        saying it twice, which is saying it wrongly."""
        hub = Hub()
        phone = connect(hub, kind=DeviceKind.IOS)
        await record(phone, minutes=30, until=local_now())
        notifier = RecordingNotifier()

        await scheduler(settings, hub, notifier).tick(local_now())

        assert notifier.sent == []

    async def test_nothing_is_said_to_a_machine_nobody_is_sitting_at(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The agent stays connected through lunch. The chair is still empty."""
        hub = Hub()
        device = connect(hub)
        await record(device, minutes=30, until=local_now(), idle=True)
        notifier = RecordingNotifier()

        await scheduler(settings, hub, notifier).tick(local_now())

        assert notifier.sent == []


class TestWhatItSays:
    async def test_the_morning_briefing_reaches_a_present_owner(self, settings) -> None:  # type: ignore[no-untyped-def]
        now = local_now()
        hub = Hub()
        device = connect(hub)
        await record(device, minutes=20, until=now)
        notifier = RecordingNotifier()
        tracker = FakeTracker([Task(id="t1", title="Созвон", when=now, at="11:00")])

        sent = await scheduler(settings, hub, notifier, tracker=tracker).tick(now)

        assert sent == 1
        assert notifier.kinds == [NotificationKind.BRIEFING]
        assert "Созвон" in notifier.sent[0][1].body

    async def test_the_activity_numbers_come_from_the_database(self, settings) -> None:  # type: ignore[no-untyped-def]
        """The evening summary's one number is the whole reason the samples are
        stored, so this is the join worth proving."""
        evening = local_now(21)
        hub = Hub()
        device = connect(hub)
        # Two sittings with a proper break between them. Ninety unbroken minutes
        # would also trip the wellness rule — correct, and not what this is
        # about.
        await record(device, minutes=45, until=evening - timedelta(hours=3))
        await record(device, minutes=45, until=evening)
        notifier = RecordingNotifier()

        await scheduler(settings, hub, notifier).tick(evening)

        assert notifier.kinds == [NotificationKind.SUMMARY]
        assert "1 ч 30 мин" in notifier.sent[0][1].body

    async def test_a_tracker_that_is_down_does_not_silence_the_rest(self, settings) -> None:  # type: ignore[no-untyped-def]
        """Sunny being unreachable is not a reason to stop telling someone they
        have been sitting for three hours."""
        now = local_now(14)
        hub = Hub()
        device = connect(hub)
        await record(device, minutes=120, until=now)
        notifier = RecordingNotifier()

        await scheduler(settings, hub, notifier, tracker=FakeTracker(broken=True)).tick(now)

        assert notifier.kinds == [NotificationKind.WELLNESS]

    async def test_each_connected_machine_is_considered_separately(self, settings) -> None:  # type: ignore[no-untyped-def]
        now = local_now()
        hub = Hub()
        here, elsewhere = connect(hub), connect(hub)
        await record(here, minutes=20, until=now)
        await record(elsewhere, minutes=20, until=now - timedelta(hours=4))
        notifier = RecordingNotifier()

        await scheduler(settings, hub, notifier).tick(now)

        assert [device for device, _ in notifier.sent] == [here], "the idle machine gets nothing"


class TestNotSayingItTwice:
    async def test_a_second_tick_repeats_nothing(self, settings) -> None:  # type: ignore[no-untyped-def]
        """This loop runs every minute. Without this it would run every minute."""
        now = local_now()
        hub = Hub()
        await record(connect(hub), minutes=20, until=now)
        notifier = RecordingNotifier()
        engine = scheduler(settings, hub, notifier)

        assert await engine.tick(now) == 1
        assert await engine.tick(now + timedelta(minutes=1)) == 0

    async def test_a_notification_that_did_not_arrive_is_not_retried_all_day(
        self,
        settings,  # type: ignore[no-untyped-def]
    ) -> None:
        """Marked as decided before it is sent. One closed lid must not become
        forty attempts, each one a row in the audit trail."""
        now = local_now()
        hub = Hub()
        await record(connect(hub), minutes=20, until=now)
        notifier = RecordingNotifier(delivers=False)
        engine = scheduler(settings, hub, notifier)

        assert await engine.tick(now) == 0
        await engine.tick(now + timedelta(minutes=1))

        assert len(notifier.sent) == 1, "it tried once"

    async def test_tomorrow_starts_over(self, settings) -> None:  # type: ignore[no-untyped-def]
        """And the memory of what was said is cleared with it, so weeks of
        uptime cannot grow it without bound."""
        now = local_now()
        hub = Hub()
        await record(connect(hub), minutes=20, until=now)
        notifier = RecordingNotifier()
        engine = scheduler(settings, hub, notifier)

        await engine.tick(now)
        tomorrow = now + timedelta(days=1)
        await record(next(iter(hub._connections)), minutes=20, until=tomorrow)

        assert await engine.tick(tomorrow) == 1


class TestTheLoopSurvives:
    async def test_a_failing_tick_does_not_stop_the_scheduler(self, settings) -> None:  # type: ignore[no-untyped-def]
        """A scheduler that dies on the first exception fails at some unrelated
        moment and the reminders simply stop, with nothing to notice."""
        hub = Hub()
        await record(connect(hub), minutes=20, until=local_now())

        class Exploding(RecordingNotifier):
            async def send(self, device_id: uuid.UUID, notification: Notify) -> bool:
                raise RuntimeError("boom")

        engine = scheduler(
            settings, hub, Exploding(), schedule=Schedule(briefing_hour=0, briefing_until_hour=24)
        )
        engine._settings = settings.model_copy(update={"proactive_interval_s": 10.0})
        engine.start()
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError):
            await engine.tick(local_now())

        await engine.stop()

    async def test_stopping_is_clean_even_if_never_started(self, settings) -> None:  # type: ignore[no-untyped-def]
        await scheduler(settings, Hub(), RecordingNotifier()).stop()


def test_priority_import_is_used() -> None:
    """Keeps the Task factory above honest about what a real task carries."""
    assert Priority.MEDIUM.value == "medium"
