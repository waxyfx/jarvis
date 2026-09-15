"""Prayer times, from arithmetic or from a timetable the owner supplied.

Two ways of knowing, and they answer different worries.

:mod:`times` computes them from the date and a pair of coordinates. Nothing
leaves the machine, it works with the internet down, and it works for any date —
but Fajr and Isha depend on which authority you follow, so the angles are a
setting the owner has to check once against their own mosque.

:mod:`schedule` reads a timetable the owner supplied, and guesses nothing at
all. That is exactly right for someone whose mosque publishes times: there is no
method to argue with. It is also bounded to the days in the file.

Where both are available the timetable wins, because it is the owner's own
authority rather than this code's approximation of it.
"""

from atlas_backend.prayer.schedule import (
    OBLIGATORY,
    DailySchedule,
    InvalidTimetableError,
    PrayerTime,
    ScheduleUnavailableError,
    Timetable,
    load_timetable,
)
from atlas_backend.prayer.times import METHODS, AsrMethod, Method, Prayer, PrayerTimes, compute
from atlas_backend.prayer.tools import (
    PRAYER_TOOLS,
    PrayerSettings,
    PrayerToolError,
    run_prayer_tool,
)

__all__ = [
    "METHODS",
    "OBLIGATORY",
    "PRAYER_TOOLS",
    "AsrMethod",
    "DailySchedule",
    "InvalidTimetableError",
    "Method",
    "Prayer",
    "PrayerSettings",
    "PrayerTime",
    "PrayerTimes",
    "PrayerToolError",
    "ScheduleUnavailableError",
    "Timetable",
    "compute",
    "load_timetable",
    "run_prayer_tool",
]
