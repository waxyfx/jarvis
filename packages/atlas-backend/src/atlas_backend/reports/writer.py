"""Gathering the day and filing it, once.

The rule is a clock and a flag: after the configured hour, on a day whose report
has not been written yet, gather and file. Deliberately not tied to the owner
being present — the day happened whether or not they are at the desk to hear
about it, and the record should exist by morning.

**Which machine's day.** The samples are per device, and merging two machines'
timelines would double-count an afternoon spent on one of them. So the report
covers the device with the most samples that day, which for one laptop is the
only device and for two is the one actually used. A second machine's day is
simply not in the report; that is a known limit rather than a silent merge.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.activity.store import samples_between, start_of_day
from atlas_backend.activity.summary import summarise
from atlas_backend.db.models import ActivitySampleRow, Reminder
from atlas_backend.db.session import Database
from atlas_backend.logging import get_logger
from atlas_backend.reports.daily import DayReport, NoteSink, build_report
from atlas_backend.tracker.provider import TrackerError, TrackerProvider

__all__ = ["DailyReportWriter"]

log = get_logger(__name__)


class DailyReportWriter:
    """Builds the day's report and files it in the tracker."""

    def __init__(
        self,
        *,
        database: Database,
        tracker: TrackerProvider | None = None,
        notes: NoteSink | None = None,
        hour: int = 22,
    ) -> None:
        self._database = database
        self._tracker = tracker
        #: Where the report goes. Usually the tracker itself, which knows how to
        #: file a note; separate so a report can be written somewhere else
        #: without the tracker having to be involved.
        self._notes = notes if notes is not None else _notes_of(tracker)
        self._hour = hour
        self._written_on: str = ""

    @property
    def available(self) -> bool:
        """Whether there is anywhere to put a report."""
        return self._notes is not None

    async def maybe_write(self, now: datetime) -> DayReport | None:
        """File today's report if it is time and it has not been filed.

        Returns what was written, or None. Never raises: this runs from the
        scheduler, and a tracker that is down at ten in the evening should cost
        one report, not the reminders for the rest of the week.
        """
        stamp = now.date().isoformat()
        if self._notes is None or now.hour < self._hour or self._written_on == stamp:
            return None

        # Marked before filing, not after. A tracker that is down would
        # otherwise be retried every minute until midnight.
        self._written_on = stamp

        try:
            report = await self.gather(now)
            await self._notes.add_note(
                title=report.title, body=report.as_body(), tags=list(report.tags)
            )
        except TrackerError as exc:
            log.warning("daily_report_failed", error=str(exc))
            return None
        except Exception:
            log.exception("daily_report_failed")
            return None

        log.info("daily_report_written", done=len(report.done), left=len(report.left))
        return report

    async def gather(self, now: datetime) -> DayReport:
        """Everything the report is made of, from the tracker and the database."""
        tasks = []
        habits = []
        if self._tracker is not None:
            try:
                tasks = list(await self._tracker.tasks_today())
                habits = list(await self._tracker.habits())
            except TrackerError as exc:
                # A report about the computer alone is still worth filing, and
                # says plainly that the task half is missing by being absent.
                log.warning("daily_report_tracker_unavailable", error=str(exc))

        async with self._database.transaction() as session:
            device_id = await _busiest_device(session, since=start_of_day(now))
            samples = (
                await samples_between(session, device_id=device_id, since=start_of_day(now))
                if device_id is not None
                else []
            )
            waiting = await _reminders_waiting(session, now=now)

        return build_report(
            on=now,
            tasks=tasks,
            habits=habits,
            activity=summarise(samples, now=now),
            watched=bool(samples),
            reminders_waiting=waiting,
        )


def _notes_of(tracker: TrackerProvider | None) -> NoteSink | None:
    """The tracker, if it can file a note.

    Checked rather than assumed because the protocol is structural and the test
    fakes are deliberately partial: a tracker that only answers questions is a
    perfectly valid tracker, it just cannot be written to.
    """
    if tracker is not None and hasattr(tracker, "add_note"):
        return tracker
    return None


async def _reminders_waiting(session: AsyncSession, *, now: datetime) -> list[str]:
    """Reminders set but not yet said. The one thing in the report still live."""
    rows = await session.execute(
        select(Reminder.text)
        .where(
            Reminder.due_at > now,
            Reminder.delivered_at.is_(None),
            Reminder.cancelled_at.is_(None),
        )
        .order_by(Reminder.due_at)
        .limit(10)
    )
    return [text for (text,) in rows.all()]


async def _busiest_device(session: AsyncSession, *, since: datetime) -> uuid.UUID | None:
    """The machine that produced the most samples today. See the module docstring."""
    row = await session.execute(
        select(ActivitySampleRow.device_id, func.count())
        .where(ActivitySampleRow.ts >= since)
        .group_by(ActivitySampleRow.device_id)
        .order_by(func.count().desc())
        .limit(1)
    )
    found = row.first()
    return found[0] if found else None
