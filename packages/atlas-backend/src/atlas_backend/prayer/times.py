"""Prayer times, computed here rather than fetched.

There are free APIs for this. Not using one is deliberate: the request would
carry the owner's coordinates and the fact that they pray to a third party,
every day, for a calculation that needs nothing but the date. It also works with
the internet down, which is the other half of why it is here.

**The arithmetic is adhanpy's, not ours.** MIT-licensed, no runtime
dependencies, and years of people checking it against real timetables. This
module was two hundred lines of hand-rolled trigonometry until a comparison
across five cities and four solstice and equinox dates showed the two agreeing
to within three minutes everywhere and within one at Almaty — at which point
keeping our own meant carrying the risk for none of the benefit. Two real bugs
had already been found in it, both by tests: an Asr angle with the wrong sign,
and an unreduced solar longitude that computed every time for a date twenty-five
days away while every time *of day* still looked correct.

The public shape did not change, which is what made the swap checkable: the same
``compute()``, the same :class:`PrayerTimes`, and the same fifty-one tests.

**The method matters more than the arithmetic.** Sunrise and sunset are
astronomy and have one right answer. Fajr and Isha are defined by how far the
sun is below the horizon, and authorities disagree by twenty minutes or more at
this latitude. The method is therefore a setting, and nothing here pretends
there is a single correct value. The owner should compare one day against their
own mosque; :data:`METHODS` says who uses each.

**Far north, a convention is applied and named.** Above roughly 48 degrees there
are summer nights when the sun never reaches the twilight angle, and every
published time for them is a rule rather than an observation. An earlier version
returned nothing at all, which is defensible and turned out to be less useful
than an answer that says which rule produced it — see :attr:`PrayerTimes.by_rule`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, tzinfo
from enum import StrEnum
from typing import Any

from adhanpy.calculation.CalculationMethod import (  # type: ignore[import-untyped]
    CalculationMethod,
)
from adhanpy.calculation.CalculationParameters import (  # type: ignore[import-untyped]
    CalculationParameters,
)
from adhanpy.calculation.HighLatitudeRule import (  # type: ignore[import-untyped]
    HighLatitudeRule,
)
from adhanpy.calculation.Madhab import Madhab  # type: ignore[import-untyped]
from adhanpy.PrayerTimes import PrayerTimes as _AdhanTimes  # type: ignore[import-untyped]

__all__ = [
    "METHODS",
    "AsrMethod",
    "HighLatitude",
    "Method",
    "Prayer",
    "PrayerTimes",
    "PrayerUnavailableError",
    "compute",
]


class PrayerUnavailableError(RuntimeError):
    """No rule produces an answer for that date and place.

    Polar day and polar night. The message never contains the coordinates.
    """


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


class HighLatitude(StrEnum):
    """What to do on a night when the sun never reaches the twilight angle.

    All three are conventions rather than observations, which is why the result
    says when one was used. Names match adhanpy's so the mapping cannot drift.
    """

    #: Fajr and Isha are placed at the middle of the night. The most common.
    MIDDLE_OF_THE_NIGHT = "middle_of_the_night"
    #: A seventh of the night either side. Gives a longer night-time window.
    SEVENTH_OF_THE_NIGHT = "seventh_of_the_night"
    #: In proportion to the angles themselves.
    TWILIGHT_ANGLE = "twilight_angle"


@dataclass(frozen=True, slots=True)
class Method:
    """How far below the horizon the sun is at Fajr and at Isha.

    ``adhan_name`` names adhanpy's own convention where it has one. Where it
    does not — Tehran — the angles are passed explicitly rather than the method
    being quietly dropped, which would move somebody's Isha by half an hour.
    """

    name: str
    fajr_angle: float
    isha_angle: float
    #: Some authorities define Isha as a fixed interval after Maghrib instead of
    #: an angle. This is the Umm al-Qura convention, Ramadan variants aside.
    isha_interval_minutes: int = 0
    adhan_name: str | None = None


#: The conventions in common use. Names are the ones people search for, so that
#: someone comparing against a printed timetable can tell which one it is.
METHODS: dict[str, Method] = {
    "mwl": Method("Muslim World League", 18.0, 17.0, adhan_name="MUSLIM_WORLD_LEAGUE"),
    "isna": Method("Islamic Society of North America", 15.0, 15.0, adhan_name="NORTH_AMERICA"),
    "egypt": Method("Egyptian General Authority of Survey", 19.5, 17.5, adhan_name="EGYPTIAN"),
    "makkah": Method(
        "Umm al-Qura, Makkah", 18.5, 0.0, isha_interval_minutes=90, adhan_name="UMM_AL_QURA"
    ),
    "karachi": Method("University of Islamic Sciences, Karachi", 18.0, 18.0, adhan_name="KARACHI"),
    # No adhanpy equivalent: the angles are supplied directly.
    "tehran": Method("Institute of Geophysics, Tehran", 17.7, 14.0),
    "dubai": Method("Dubai", 18.2, 18.2, adhan_name="DUBAI"),
    "qatar": Method("Qatar", 18.0, 0.0, isha_interval_minutes=90, adhan_name="QATAR"),
    "kuwait": Method("Kuwait", 18.0, 17.5, adhan_name="KUWAIT"),
    "singapore": Method("Singapore", 20.0, 18.0, adhan_name="SINGAPORE"),
}


@dataclass(frozen=True, slots=True)
class PrayerTimes:
    """One day's times, in the owner's own timezone."""

    on: date
    fajr: datetime | None
    sunrise: datetime | None
    dhuhr: datetime
    asr: datetime | None
    maghrib: datetime | None
    isha: datetime | None
    #: Prayers whose time today came from a high-latitude convention rather than
    #: from the sun actually reaching the angle. Empty almost everywhere.
    by_rule: tuple[Prayer, ...] = field(default=())

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


# ---------------------------------------------------------------- computing


def _parameters(
    method: Method, asr: AsrMethod, high_latitude: HighLatitude
) -> CalculationParameters:
    """This module's vocabulary, in adhanpy's terms."""
    if method.adhan_name is None:
        parameters = CalculationParameters(
            fajr_angle=method.fajr_angle, isha_angle=method.isha_angle
        )
    else:
        parameters = CalculationParameters(method=CalculationMethod[method.adhan_name])

    if method.isha_interval_minutes:
        parameters.isha_interval = method.isha_interval_minutes
    parameters.madhab = Madhab.HANAFI if asr is AsrMethod.HANAFI else Madhab.SHAFI
    parameters.high_latitude_rule = HighLatitudeRule[high_latitude.name]
    return parameters


def compute(
    on: date,
    *,
    latitude: float,
    longitude: float,
    zone: tzinfo,
    method: Method | str = "mwl",
    asr: AsrMethod = AsrMethod.STANDARD,
    high_latitude: HighLatitude = HighLatitude.MIDDLE_OF_THE_NIGHT,
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
    parameters = _parameters(resolved, asr, high_latitude)

    def at(day: date) -> Any:
        return _AdhanTimes(
            (latitude, longitude),
            datetime.combine(day, time(), UTC),
            calculation_parameters=parameters,
        )

    try:
        raw = at(on)
        # adhanpy anchors on the solar day, which near the date line is not the
        # civil one. Correcting by local noon rather than by Fajr keeps the whole
        # set on the day the caller asked about.
        drift = on - raw.dhuhr.astimezone(zone).date()
        if drift:
            raw = at(on + drift)
        found = {
            prayer: _to_the_minute(getattr(raw, prayer.value), zone)
            for prayer in Prayer
            if getattr(raw, prayer.value, None) is not None
        }
    except (ArithmeticError, AttributeError, RuntimeError, TypeError, ValueError) as exc:
        # Polar day and polar night: no rule produces an answer, so none is
        # invented. adhanpy signals it with a bare RuntimeError, which is why
        # that is in the list. The coordinates are deliberately not in the
        # message.
        raise PrayerUnavailableError(
            "no prayer times can be computed for that date and place"
        ) from exc

    if Prayer.DHUHR not in found:
        raise PrayerUnavailableError("no prayer times can be computed for that date and place")

    return PrayerTimes(
        on=on,
        fajr=found.get(Prayer.FAJR),
        sunrise=found.get(Prayer.SUNRISE),
        dhuhr=found[Prayer.DHUHR],
        asr=found.get(Prayer.ASR),
        maghrib=found.get(Prayer.MAGHRIB),
        isha=found.get(Prayer.ISHA),
        by_rule=_by_convention(on, latitude, longitude, resolved, high_latitude),
    )


#: Below this latitude the sun reaches every twilight angle on every night of
#: the year, so no rule can apply and the check below is skipped.
_RULES_NEVER_APPLY_BELOW = 45.0

#: Two different conventions landing this close together means neither was
#: needed — the sun itself decided.
_SAME_ANSWER_WITHIN_S = 120.0


def _by_convention(
    on: date,
    latitude: float,
    longitude: float,
    method: Method,
    high_latitude: HighLatitude,
) -> tuple[Prayer, ...]:
    """Which of today's prayers are a rule rather than an observation.

    Worth saying out loud. "Fajr is at 00:16" reads as a fact; "Fajr is at 00:16
    by the middle-of-the-night rule, because the sun never got that far below
    the horizon" is the truth, and the difference decides whether to trust it.

    Detected by asking for the same day under two *different* conventions: where
    the sun genuinely reaches the angle both give the same answer, and where it
    does not they disagree by a great deal.
    """
    if abs(latitude) < _RULES_NEVER_APPLY_BELOW:
        return ()

    moment = datetime.combine(on, time(), UTC)
    answers = []
    for rule in (HighLatitudeRule.TWILIGHT_ANGLE, HighLatitudeRule.SEVENTH_OF_THE_NIGHT):
        parameters = CalculationParameters(
            fajr_angle=method.fajr_angle, isha_angle=method.isha_angle
        )
        parameters.high_latitude_rule = rule
        try:
            answers.append(
                _AdhanTimes((latitude, longitude), moment, calculation_parameters=parameters)
            )
        except Exception:
            return ()

    one, two = answers
    return tuple(
        prayer
        for prayer in (Prayer.FAJR, Prayer.ISHA)
        if abs((getattr(one, prayer.value) - getattr(two, prayer.value)).total_seconds())
        > _SAME_ANSWER_WITHIN_S
    )


def _to_the_minute(moment: datetime, zone: tzinfo) -> datetime:
    """Local time, with the seconds dropped.

    Nobody reads prayer times to the second, and a displayed 05:59:47 that a
    reminder treats as 06:00 is a discrepancy with no upside.
    """
    return moment.astimezone(zone).replace(second=0, microsecond=0)
