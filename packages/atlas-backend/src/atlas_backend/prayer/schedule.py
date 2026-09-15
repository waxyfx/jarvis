"""Read an explicitly supplied timetable; never guess a city or calculate a rite.

Inputs are data, not instructions. This module has no network, filesystem,
model, policy or notification access. A future tool handler must go through the
ordinary catalogue/dispatcher and pass its result as untrusted tool content.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from atlas_backend.prayer.times import Prayer

_MAX_DOCUMENT_BYTES = 512_000
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class InvalidTimetableError(ValueError):
    """Invalid local data. The public message never echoes document contents."""


class ScheduleUnavailableError(LookupError):
    """The requested date is not covered. No extrapolation is performed."""


#: The five obligatory prayers, in the order a day runs. Drawn from the shared
#: enum rather than declared again here: the package already had one vocabulary
#: for these names and two would eventually disagree. Sunrise is in that enum
#: because a computed timetable reports it, and is absent here because it is a
#: boundary rather than something a published timetable lists.
OBLIGATORY: tuple[Prayer, ...] = (
    Prayer.FAJR,
    Prayer.DHUHR,
    Prayer.ASR,
    Prayer.MAGHRIB,
    Prayer.ISHA,
)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PrayerTime(_Record):
    prayer: Prayer
    at: AwareDatetime

    @field_validator("at", mode="before")
    @classmethod
    def explicit_datetime(cls, value: object) -> object:
        # Pydantic also accepts numeric epochs. A human-supplied timetable must
        # make its date and UTC offset explicit instead.
        if not isinstance(value, (str, datetime)):
            raise ValueError("use an ISO datetime with a UTC offset")
        if isinstance(value, str) and "T" not in value:
            raise ValueError("use an ISO datetime with a UTC offset")
        return value


class DailySchedule(_Record):
    day: date
    times: tuple[PrayerTime, ...] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def ordered_prayers(self) -> Self:
        if tuple(item.prayer for item in self.times) != OBLIGATORY:
            raise ValueError("each of the five prayers is required, in chronological order")
        instants = tuple(item.at.astimezone(UTC) for item in self.times)
        if any(left >= right for left, right in pairwise(instants)):
            raise ValueError("prayer instants must be strictly increasing")
        return self


def _zone(key: str) -> ZoneInfo:
    try:
        return ZoneInfo(key)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("an available IANA timezone is required; install tzdata") from exc


class Timetable(_Record):
    """One locality, one declared source/method, up to 400 explicit civil days.

    ``source`` is an attribution label, not a verified identity or a URL to
    fetch. ``method`` records the supplier's convention, never an AI choice.
    This first format requires all five times to fall on their civil day;
    after-midnight Isha schedules are rejected, not silently shifted.
    """

    locality: Label
    timezone: Label
    source: Label
    method: Label
    revision: Label
    days: tuple[DailySchedule, ...] = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def consistent_dates_and_offsets(self) -> Self:
        zone = _zone(self.timezone)
        previous: date | None = None
        for schedule in self.days:
            if previous is not None and schedule.day <= previous:
                raise ValueError("schedule dates must be unique and increasing")
            previous = schedule.day
            for item in schedule.times:
                # A UTC round trip catches nonexistent DST wall times, including
                # Python datetime instances created by merely attaching tzinfo.
                local = item.at.astimezone(UTC).astimezone(zone)
                if (
                    item.at.date() != schedule.day
                    or local.date() != schedule.day
                    or local.replace(tzinfo=None) != item.at.replace(tzinfo=None)
                    or local.utcoffset() != item.at.utcoffset()
                ):
                    raise ValueError("time must match the schedule date and timezone offset")
        return self

    def for_day(self, day: date) -> DailySchedule:
        for schedule in self.days:
            if schedule.day == day:
                return schedule
        raise ScheduleUnavailableError("no timetable for the requested local date")

    def next_prayer(self, *, now: datetime) -> PrayerTime:
        """First time at or after now; an exact boundary is still due now.

        Consult today's explicit times, then tomorrow's. Never jump across a
        missing date or recycle yesterday's times as if they applied today.
        Comparisons use UTC, so DST folds cannot reverse chronological order.
        """
        if now.utcoffset() is None:
            raise ValueError("now must have a UTC offset")
        instant = now.astimezone(UTC)
        today = instant.astimezone(_zone(self.timezone)).date()
        for item in self.for_day(today).times:
            if item.at.astimezone(UTC) >= instant:
                return item
        if today == date.max:
            raise ScheduleUnavailableError("no next local date is representable")
        return self.for_day(today + timedelta(days=1)).times[0]


def load_timetable(document: str | bytes) -> Timetable:
    """Parse bounded JSON already read by trusted application code.

    Do not expose an arbitrary path or URL argument to the model. Keep local
    files outside Git. Validation errors are replaced with a fixed message so
    a malformed document cannot echo personal details into logs or replies.
    """
    try:
        raw = document.encode("utf-8") if isinstance(document, str) else document
        if len(raw) > _MAX_DOCUMENT_BYTES:
            raise InvalidTimetableError("timetable exceeds the document size limit")
        return Timetable.model_validate_json(raw)
    except (ValidationError, UnicodeError):
        raise InvalidTimetableError("invalid prayer timetable") from None
