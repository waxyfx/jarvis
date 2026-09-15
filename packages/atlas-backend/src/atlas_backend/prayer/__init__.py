"""Prayer times, computed on this machine.

Free APIs exist. Not using one is the point: the request would carry the
owner's coordinates and the fact that they pray to a third party, every day,
for a calculation that needs nothing but the date.
"""

from atlas_backend.prayer.times import METHODS, AsrMethod, Method, Prayer, PrayerTimes, compute
from atlas_backend.prayer.tools import (
    PRAYER_TOOLS,
    PrayerSettings,
    PrayerToolError,
    run_prayer_tool,
)

__all__ = [
    "METHODS",
    "PRAYER_TOOLS",
    "AsrMethod",
    "Method",
    "Prayer",
    "PrayerSettings",
    "PrayerTimes",
    "PrayerToolError",
    "compute",
    "run_prayer_tool",
]
