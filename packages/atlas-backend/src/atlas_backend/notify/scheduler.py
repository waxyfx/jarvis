"""The clock that gives the rules a chance to speak.

One task, one minute at a time, for each connected agent. Everything expensive
is behind a connection check: with the laptop shut this loop does nothing but
wake up and go back to sleep, which is most of the day.

**It never lets a failure stop it.** A tracker that is down, a database blip, a
rule with a bug in it — each of those is one quiet minute, logged, and the loop
continues. A scheduler that dies on the first exception is worse than no
scheduler, because it fails at some unrelated moment and the reminders simply
stop, with nothing to notice.

**What it has already said is kept in memory only.** A restart can therefore
repeat a briefing, which is the mild failure; the alternative — a table, a
migration and a write on every tick — buys very little for a single-owner
system. Written down as a known limitation rather than discovered as a bug:
see docs/M5-REPORT.md.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from atlas_backend.activity.store import samples_between, start_of_day
from atlas_backend.activity.summary import ActivityDigest, Sample, summarise
from atlas_backend.config import Settings
from atlas_backend.db.models import Reminder
from atlas_backend.db.session import Database
from atlas_backend.logging import get_logger
from atlas_backend.notify.notifier import Notifier
from atlas_backend.notify.rules import Moment, Schedule, decide_all
from atlas_backend.prayer.tools import PrayerSettings
from atlas_backend.reports.writer import DailyReportWriter
from atlas_backend.tracker.provider import Task, TrackerError, TrackerProvider
from atlas_backend.ws.hub import Hub
from atlas_shared.enums import DeviceKind

__all__ = ["ProactiveScheduler"]

log = get_logger(__name__)

#: How recent the last activity sample has to be for the owner to count as
#: present. Two sampling intervals, so one dropped batch is not read as an
#: empty chair.
_PRESENT_WITHIN_S = 30.0

#: Reminders delivered in one tick. A backend that was down for a day comes
#: back to a pile of them, and saying forty things in a row is not a
#: reminder — it is a reason to switch this off.
_MOST_AT_ONCE = 3


class ProactiveScheduler:
    """Wakes up, asks the rules whether anything is worth saying, says it."""

    def __init__(
        self,
        *,
        hub: Hub,
        notifier: Notifier,
        database: Database,
        settings: Settings,
        tracker: TrackerProvider | None = None,
        prayer: PrayerSettings | None = None,
        schedule: Schedule | None = None,
    ) -> None:
        self._hub = hub
        self._notifier = notifier
        self._database = database
        self._settings = settings
        self._tracker = tracker
        #: Absent unless the owner has given a location. Absent means the
        #: prayer rule has nothing to say, not that it is broken.
        self._prayer = prayer
        self._schedule = schedule or Schedule(
            briefing_hour=settings.briefing_hour,
            briefing_until_hour=settings.briefing_until_hour,
            evening_hour=settings.evening_summary_hour,
            evening_until_hour=settings.evening_summary_until_hour,
            long_session_minutes=settings.long_session_minutes,
            prayer_reminder_minutes=settings.prayer_reminder_minutes,
            quiet_from_hour=settings.quiet_from_hour,
            quiet_until_hour=settings.quiet_until_hour,
        )
        self._zone = _zone_or_utc(settings.owner_timezone)
        #: The written report, which shares this loop but not its rules: it does
        #: not need anyone to be present and it is filed rather than spoken.
        self._report = (
            DailyReportWriter(database=database, tracker=tracker, hour=settings.daily_report_hour)
            if settings.daily_report_enabled
            else None
        )
        #: What has already been said, by key. Cleared when the day changes, so
        #: it cannot grow without bound over an uptime measured in weeks.
        self._said: set[str] = set()
        self._said_on: str = ""
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="proactive-scheduler")
            log.info("scheduler_started", interval_s=self._settings.proactive_interval_s)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._settings.proactive_interval_s)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One quiet minute, and the loop continues. See the module
                # docstring for why this is broad on purpose.
                log.exception("scheduler_tick_failed")

    # ------------------------------------------------------------------ work

    async def tick(self, now: datetime | None = None) -> int:
        """One pass. Returns how many notifications were sent.

        Public so a test can drive it without waiting for a clock, and so the
        loop above stays three lines long.
        """
        moment_now = (now or datetime.now(UTC)).astimezone(self._zone)
        self._forget_yesterday(moment_now)

        sent = 0
        for device_id in self._agents():
            sent += await self._tick_device(device_id, moment_now)

        # After the notifications, and independent of them: a report is filed
        # whether or not anyone was at the machine to be spoken to.
        if self._report is not None:
            await self._report.maybe_write(moment_now)
        return sent

    def _agents(self) -> list[uuid.UUID]:
        """Connected Windows agents. Nothing is said to a phone this way — the
        phone has its own notifications, and saying it twice is saying it
        wrongly."""
        return [
            connection.device_id
            for connection in self._hub.snapshot()
            if connection.device_kind == DeviceKind.WINDOWS_AGENT.value
        ]

    async def _tick_device(self, device_id: uuid.UUID, now: datetime) -> int:
        activity, present = await self._activity(device_id, now)
        moment = Moment(
            now=now,
            tasks=await self._tasks(),
            due_reminders=await self._due_reminders(device_id, now),
            prayers=self._prayer.times(now) if self._prayer is not None else None,
            activity=activity,
            present=present,
        )

        sent = 0
        for planned in decide_all(moment, self._schedule, already_said=self._said):
            # Marked before sending, not after. A notification that failed to
            # deliver has still been decided, and re-deciding it every minute
            # for the rest of the day is how one closed lid becomes forty
            # attempts.
            self._said.add(planned.key)
            if await self._notifier.send(device_id, planned.notification):
                sent += 1
        return sent

    async def _due_reminders(self, device_id: uuid.UUID, now: datetime) -> list[tuple[str, str]]:
        """What the owner asked to be told, that is due and not yet said.

        Marked delivered in the same transaction that reads it. The alternative
        — mark after the notification is sent — loses a reminder when the send
        fails and repeats one when it half-succeeds, and of those two a reminder
        that arrives once and might be missed beats one that arrives eleven
        times.
        """
        async with self._database.transaction() as session:
            rows = await session.execute(
                select(Reminder)
                .where(
                    Reminder.device_id == device_id,
                    Reminder.due_at <= now,
                    Reminder.delivered_at.is_(None),
                    Reminder.cancelled_at.is_(None),
                )
                .order_by(Reminder.due_at)
                .limit(_MOST_AT_ONCE)
            )
            due = list(rows.scalars())
            for reminder in due:
                reminder.delivered_at = now

        return [(str(reminder.id), reminder.text) for reminder in due]

    async def _tasks(self) -> Sequence[Task]:
        if self._tracker is None:
            return ()
        try:
            return await self._tracker.tasks_today()
        except TrackerError as exc:
            # The tracker being down is not a reason to stop warning someone
            # they have been sitting for three hours.
            log.warning("scheduler_tracker_unavailable", error=str(exc))
            return ()

    async def _activity(self, device_id: uuid.UUID, now: datetime) -> tuple[ActivityDigest, bool]:
        async with self._database.transaction() as session:
            samples = await samples_between(session, device_id=device_id, since=start_of_day(now))
        return summarise(samples, now=now), _is_present(samples, now)

    def _forget_yesterday(self, now: datetime) -> None:
        stamp = now.date().isoformat()
        if stamp != self._said_on:
            self._said.clear()
            self._said_on = stamp


def _zone_or_utc(name: str) -> ZoneInfo:
    """The owner's timezone, or UTC with a complaint.

    Two ways this fails and neither should stop the backend from starting.
    A typo in the setting is one. The other is Windows, which ships no IANA
    database at all — `tzdata` is a declared dependency for exactly that reason,
    and an environment that somehow lacks it would otherwise take the whole
    service down over the morning briefing being an hour out.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.error("unknown_timezone", configured=name, using="UTC")
        return ZoneInfo("UTC")


def _is_present(samples: Sequence[Sample], now: datetime) -> bool:
    """Whether someone is actually at the machine.

    An agent can be connected with nobody in the room — that is most of a
    lunch break. Speaking to an empty chair wastes the once-a-day notifications
    on nobody, so presence is the last sample being both recent and not idle.
    """
    if not samples:
        return False
    last = samples[-1]
    return not last.is_idle and (now - last.ts).total_seconds() <= _PRESENT_WITHIN_S
