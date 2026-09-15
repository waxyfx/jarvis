"""Answering "во сколько намаз?".

One tool. The times are computed here from the date and a pair of coordinates,
so this is the rare backend tool with nothing behind it at all: no network, no
database, no third party told where the owner is or that they pray.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from atlas_backend.prayer.times import AsrMethod, Prayer, PrayerTimes, compute

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
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.zone = zone
        self.method = method
        self.asr = asr

    def times(self, on: datetime) -> PrayerTimes:
        return compute(
            on.date(),
            latitude=self.latitude,
            longitude=self.longitude,
            zone=self.zone,
            method=self.method,
            asr=self.asr,
        )


def _today(settings: PrayerSettings, now: datetime, _: Mapping[str, Any]) -> dict[str, Any]:
    times = settings.times(now)
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

    missing = [
        prayer.value
        for prayer in (Prayer.FAJR, Prayer.ISHA)
        if getattr(times, prayer.value) is None
    ]
    if missing:
        # Not an error and not a gap to paper over: this far north there are
        # summer nights when the sun never reaches the twilight angle.
        result["not_defined_today"] = missing

    return result


PRAYER_TOOLS = {"prayer.today": _today}


def run_prayer_tool(
    settings: PrayerSettings, now: datetime, name: str, args: Mapping[str, Any]
) -> dict[str, Any]:
    handler = PRAYER_TOOLS.get(name)
    if handler is None:
        raise PrayerToolError(f"{name} is not something I can tell you about")
    return handler(settings, now, args)
