"""Prayer times, computed here rather than fetched.

There are free APIs for this. Not using one is deliberate: the request would
carry the owner's coordinates and the fact that they pray to a third party, every
day, for a calculation that is a page of trigonometry and needs nothing but the
date. It also works with the internet down, which is the other half of why it is
here.

**The method matters more than the arithmetic.** Sunrise and sunset are
astronomy and have one right answer. Fajr and Isha are defined by how far the
sun is below the horizon, and different authorities use different angles — the
spread between them is twenty minutes or more at this latitude. The angle is
therefore a setting, and nothing here pretends there is a single correct value.
The owner should compare one day's output against their own mosque and adjust;
:data:`METHODS` carries the common conventions and says who uses each.

**High latitudes are refused, not guessed.** Above roughly 48 degrees there are
nights in summer when the sun never reaches the twilight angle at all, and every
answer is a convention rather than a fact. Returning `None` for those prayers is
honest; inventing a time is not.

The algorithm is the standard one — solar position, equation of time, hour
angles — as published by PrayTimes.org and used by most implementations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum

__all__ = ["METHODS", "AsrMethod", "Method", "Prayer", "PrayerTimes", "compute"]


class Prayer(StrEnum):
    """The five, plus sunrise — which is not a prayer but ends Fajr."""

    FAJR = "fajr"
    SUNRISE = "sunrise"
    DHUHR = "dhuhr"
    ASR = "asr"
    MAGHRIB = "maghrib"
    ISHA = "isha"

    @property
    def is_prayer(self) -> bool:
        return self is not Prayer.SUNRISE

    @property
    def russian(self) -> str:
        return _RUSSIAN[self]


_RUSSIAN = {
    Prayer.FAJR: "Фаджр",
    Prayer.SUNRISE: "Восход",
    Prayer.DHUHR: "Зухр",
    Prayer.ASR: "Аср",
    Prayer.MAGHRIB: "Магриб",
    Prayer.ISHA: "Иша",
}


class AsrMethod(StrEnum):
    """When the Asr shadow has grown enough.

    Two schools, and the difference is roughly an hour. Not a detail to guess
    at on someone's behalf.
    """

    #: Shafi'i, Maliki, Hanbali: shadow equal to the object's own length.
    STANDARD = "standard"
    #: Hanafi: twice the object's length.
    HANAFI = "hanafi"

    @property
    def shadow_factor(self) -> int:
        return 2 if self is AsrMethod.HANAFI else 1


@dataclass(frozen=True, slots=True)
class Method:
    """How far below the horizon the sun is at Fajr and at Isha."""

    name: str
    fajr_angle: float
    isha_angle: float
    #: Some authorities define Isha as a fixed interval after Maghrib instead of
    #: an angle. Ramadan variants aside, this is the Umm al-Qura convention.
    isha_interval_minutes: int = 0


#: The conventions in common use. Names are the ones people search for, so that
#: someone comparing against a printed timetable can tell which one it is.
METHODS: dict[str, Method] = {
    "mwl": Method("Muslim World League", fajr_angle=18.0, isha_angle=17.0),
    "isna": Method("Islamic Society of North America", fajr_angle=15.0, isha_angle=15.0),
    "egypt": Method("Egyptian General Authority of Survey", fajr_angle=19.5, isha_angle=17.5),
    "makkah": Method(
        "Umm al-Qura, Makkah", fajr_angle=18.5, isha_angle=0.0, isha_interval_minutes=90
    ),
    "karachi": Method("University of Islamic Sciences, Karachi", fajr_angle=18.0, isha_angle=18.0),
    "tehran": Method("Institute of Geophysics, Tehran", fajr_angle=17.7, isha_angle=14.0),
}

#: The sun's centre this far below the horizon at sunrise and sunset: half the
#: solar disc plus atmospheric refraction.
_HORIZON = 0.833


@dataclass(frozen=True, slots=True)
class PrayerTimes:
    """One day's times, in the owner's own timezone.

    A value of ``None`` means the sun never reached that angle: a real answer
    for a summer night this far north, and better than a number nobody can pray
    by.
    """

    on: date
    fajr: datetime | None
    sunrise: datetime | None
    dhuhr: datetime
    asr: datetime | None
    maghrib: datetime | None
    isha: datetime | None

    def items(self) -> list[tuple[Prayer, datetime]]:
        """Everything that has a time, in the order the day runs."""
        found = [
            (Prayer.FAJR, self.fajr),
            (Prayer.SUNRISE, self.sunrise),
            (Prayer.DHUHR, self.dhuhr),
            (Prayer.ASR, self.asr),
            (Prayer.MAGHRIB, self.maghrib),
            (Prayer.ISHA, self.isha),
        ]
        return [(prayer, moment) for prayer, moment in found if moment is not None]

    def prayers(self) -> list[tuple[Prayer, datetime]]:
        """The five. Sunrise is a boundary, not something to be reminded of."""
        return [(prayer, moment) for prayer, moment in self.items() if prayer.is_prayer]

    def next_after(self, moment: datetime) -> tuple[Prayer, datetime] | None:
        for prayer, at in self.prayers():
            if at > moment:
                return (prayer, at)
        return None

    def as_result(self) -> dict[str, str]:
        """Shaped for the model: names it can say, times it can read out."""
        return {prayer.value: at.strftime("%H:%M") for prayer, at in self.items()}


# --------------------------------------------------------------- astronomy


def _julian_day(on: date) -> float:
    year, month = on.year, on.month
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + on.day + b - 1524.5
    )


@dataclass(frozen=True, slots=True)
class _Sun:
    declination: float
    equation_of_time: float


def _sun(julian_day: float) -> _Sun:
    """Declination and equation of time, both in the usual units.

    Low-precision formulae from the Astronomical Almanac: good to well under a
    minute, which is finer than the disagreement between methods and finer than
    anyone's watch.
    """
    days = julian_day - 2451545.0

    # Both angles are reduced to a single turn before anything is done with
    # them. The sines do not care, but the equation of time below is the mean
    # longitude *in hours* minus a right ascension already folded into [0, 24) —
    # so an unreduced longitude makes it hundreds of hours wrong.
    #
    # The failure that causes is worth describing, because it hides: every time
    # of day still came out correct, since the error was very nearly a whole
    # number of days. Only the date was wrong, by twenty-five of them, and a
    # timetable printed as HH:MM shows none of that.
    mean_anomaly = math.radians(_fix_angle(357.529 + 0.98560028 * days))
    mean_longitude = _fix_angle(280.459 + 0.98564736 * days)
    apparent_longitude = math.radians(
        _fix_angle(
            mean_longitude + 1.915 * math.sin(mean_anomaly) + 0.020 * math.sin(2 * mean_anomaly)
        )
    )
    obliquity = math.radians(23.439 - 0.00000036 * days)

    right_ascension = math.degrees(
        math.atan2(math.cos(obliquity) * math.sin(apparent_longitude), math.cos(apparent_longitude))
    )
    declination = math.degrees(math.asin(math.sin(obliquity) * math.sin(apparent_longitude)))

    return _Sun(
        declination=declination,
        equation_of_time=(mean_longitude / 15 - _fix_hours(right_ascension / 15)),
    )


def _fix_hours(hours: float) -> float:
    return hours - 24.0 * math.floor(hours / 24.0)


def _fix_angle(degrees: float) -> float:
    return degrees - 360.0 * math.floor(degrees / 360.0)


def _hour_angle(angle: float, latitude: float, declination: float) -> float | None:
    """Hours between solar noon and the sun being ``angle`` below the horizon.

    ``None`` when the sun never gets there — the case this whole module refuses
    to guess at.
    """
    latitude_r = math.radians(latitude)
    declination_r = math.radians(declination)
    numerator = -math.sin(math.radians(angle)) - math.sin(latitude_r) * math.sin(declination_r)
    denominator = math.cos(latitude_r) * math.cos(declination_r)

    if denominator == 0:
        return None
    cosine = numerator / denominator
    if not -1.0 <= cosine <= 1.0:
        return None
    return math.degrees(math.acos(cosine)) / 15.0


def _asr_angle(latitude: float, declination: float, shadow_factor: int) -> float:
    """Where the sun sits when a shadow has grown to ``shadow_factor`` lengths.

    Returned **negated**, because :func:`_hour_angle` is written in terms of how
    far below the horizon the sun is and this is an altitude above it. Getting
    that sign wrong is not subtle once you look at the output — Asr came out at
    20:33, two and a half hours after sunset, and the Hanafi time landed earlier
    than the standard one instead of an hour later — but it is entirely
    invisible in the formula.
    """
    cotangent = shadow_factor + abs(math.tan(math.radians(latitude - declination)))
    return -math.degrees(math.atan(1.0 / cotangent))


def compute(
    on: date,
    *,
    latitude: float,
    longitude: float,
    zone: tzinfo,
    method: Method | str = "mwl",
    asr: AsrMethod = AsrMethod.STANDARD,
) -> PrayerTimes:
    """One day of prayer times for one place.

    ``zone`` is a real timezone rather than an offset, so the answer is right
    across a DST boundary in the places that still have one.
    """
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"latitude {latitude} is not on this planet")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"longitude {longitude} is not on this planet")

    resolved = METHODS[method] if isinstance(method, str) else method

    # The sun is computed for local noon rather than midnight: the error in the
    # low-precision formulae is smallest near the moment everything is measured
    # from, and every time here is an offset from solar noon.
    offset_hours = _offset_hours(on, zone)
    julian_day = _julian_day(on) - longitude / 360.0
    sun = _sun(julian_day)

    # Solar noon, then corrected from mean solar time at this longitude to the
    # clock the owner actually reads.
    noon = 12.0 - sun.equation_of_time - longitude / 15.0 + offset_hours

    def at(hours: float | None) -> datetime | None:
        return _moment(on, hours, zone) if hours is not None else None

    sunrise_span = _hour_angle(_HORIZON, latitude, sun.declination)
    fajr_span = _hour_angle(resolved.fajr_angle, latitude, sun.declination)
    asr_span = _hour_angle(
        _asr_angle(latitude, sun.declination, asr.shadow_factor), latitude, sun.declination
    )

    maghrib = at(noon + sunrise_span) if sunrise_span is not None else None

    isha: datetime | None
    if resolved.isha_interval_minutes and maghrib is not None:
        isha = maghrib + timedelta(minutes=resolved.isha_interval_minutes)
    else:
        isha_span = _hour_angle(resolved.isha_angle, latitude, sun.declination)
        isha = at(noon + isha_span) if isha_span is not None else None

    return PrayerTimes(
        on=on,
        fajr=at(noon - fajr_span) if fajr_span is not None else None,
        sunrise=at(noon - sunrise_span) if sunrise_span is not None else None,
        # Dhuhr is a few minutes after astronomical noon by convention, so that
        # the sun has demonstrably passed the meridian.
        dhuhr=_moment(on, noon + 1.0 / 60.0, zone),
        asr=at(noon + asr_span) if asr_span is not None else None,
        maghrib=maghrib,
        isha=isha,
    )


def _offset_hours(on: date, zone: tzinfo) -> float:
    """The zone's offset on this date, in hours, DST included."""
    reference = datetime.combine(on, time(12, 0)).replace(tzinfo=zone)
    offset = reference.utcoffset()
    return offset.total_seconds() / 3600.0 if offset else 0.0


def _moment(on: date, hours: float, zone: tzinfo) -> datetime:
    """Turn "14.37 hours into the day" into a moment, rounded to the minute.

    Seconds are dropped rather than rounded away quietly: nobody reads prayer
    times to the second, and a displayed 05:59:47 that a reminder treats as
    06:00 is a discrepancy with no upside.
    """
    whole = timedelta(hours=hours)
    start = datetime.combine(on, time(0, 0)).replace(tzinfo=zone)
    moment = start + whole
    return moment.replace(second=0, microsecond=0)
