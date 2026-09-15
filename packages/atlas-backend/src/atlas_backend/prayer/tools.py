"""Answering "во сколько намаз?".

One tool. The times are computed here from the date and a pair of coordinates,
so this is the rare backend tool with nothing behind it at all: no network, no
database, no third party told where the owner is or that they pray.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from atlas_backend.prayer.times import (
    AsrMethod,
    HighLatitude,
    PrayerTimes,
    PrayerUnavailableError,
    compute,
)

__all__ = ["PRAYER_TOOLS", "PrayerSettings", "PrayerToolError", "run_prayer_tool"]


class PrayerToolError(RuntimeError):
    """Something to say out loud, rather than something to crash on."""


class PrayerSettings:
    """Where the owner is, and whose reckoning they follow.

    Held as an object rather than read from global settings at each call so the
    tests can ask about somewhere else without touching the environment.
    """

    def __init__(
        self,
        *,
        latitude: float,
        longitude: float,
        zone: Any,
        method: str = "mwl",
        asr: AsrMethod = AsrMethod.STANDARD,
        high_latitude: HighLatitude = HighLatitude.MIDDLE_OF_THE_NIGHT,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.zone = zone
        self.method = method
        self.asr = asr
        self.high_latitude = high_latitude

    def times(self, on: datetime) -> PrayerTimes:
        return compute(
            on.date(),
            latitude=self.latitude,
            longitude=self.longitude,
            zone=self.zone,
            method=self.method,
            asr=self.asr,
            high_latitude=self.high_latitude,
        )


def _today(settings: PrayerSettings, now: datetime, _: Mapping[str, Any]) -> dict[str, Any]:
    try:
        times = settings.times(now)
    except PrayerUnavailableError as exc:
        # Polar day or polar night. Not a crash and not an empty answer that
        # looks like "no prayers today".
        raise PrayerToolError(str(exc)) from exc
    result: dict[str, Any] = dict(times.as_result())

    upcoming = times.next_after(now)
    if upcoming is not None:
        prayer, at = upcoming
        # The one thing a person actually wants when they ask. Without it the
        # model reads six times aloud and the listener does the arithmetic.
        result["next"] = {
            "prayer": prayer.value,
            "name": prayer.russian,
            "at": at.strftime("%H:%M"),
            "in_minutes": max(0, round((at - now).total_seconds() / 60)),
        }

    if times.by_rule:
        # Said rather than hidden. This far north there are summer nights when
        # the sun never reaches the twilight angle, and the time in the table is
        # then a scholarly convention rather than an observation. "Fajr is at
        # 00:16" reads as a fact; knowing a rule produced it is what lets the
        # owner decide whether to trust it.
        result["by_convention"] = [prayer.value for prayer in times.by_rule]

    return result


PRAYER_TOOLS = {"prayer.today": _today}


def run_prayer_tool(
    settings: PrayerSettings, now: datetime, name: str, args: Mapping[str, Any]
) -> dict[str, Any]:
    handler = PRAYER_TOOLS.get(name)
    if handler is None:
        raise PrayerToolError(f"{name} is not something I can tell you about")
    return handler(settings, now, args)
