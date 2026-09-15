"""Turning ten-second samples into an answer to "how long did I work today?".

The agent reports what is in the foreground and whether the owner has touched
the machine, every ten seconds. Thousands of rows a day, and none of them is an
answer. This is the arithmetic that turns them into one.

**Time is measured between samples, not counted in samples.** Multiplying a row
count by an assumed interval is wrong the moment the interval changes, the agent
reconnects, or the laptop sleeps — and wrong in the direction that invents
hours. Each gap between consecutive samples is counted, and a gap longer than
:data:`_MAX_GAP_S` is not counted at all, because nothing was observed across
it: the machine was off, asleep, or the agent was disconnected.

**Idle is not work.** Sitting at a desk with the screen on is not the same as
working, and the owner asking "сколько я сегодня работал" means the second one.
Both numbers are reported, because the difference between them is sometimes the
interesting part.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any

__all__ = ["ActivityDigest", "Sample", "friendly_name", "summarise"]

#: The longest gap between two samples that still counts as elapsed time. Above
#: this, nothing was observed and nothing is assumed — a laptop closed at 18:00
#: and opened at 09:00 must not read as fifteen hours at the desk.
_MAX_GAP_S = 120.0

#: A pause long enough to end a stretch of work. Shorter than this is a coffee,
#: not a break, and splitting on it would report four "sessions" for one
#: afternoon.
_BREAK_S = 600.0

#: How many applications are named. Read aloud, three is a list; ten is a log.
_TOP_APPS = 3

#: Process names are what Windows reports. These are what a person calls them.
#: Only the ones that are genuinely unrecognisable belong here — everything else
#: is better served by stripping the extension, which needs no maintenance.
_FRIENDLY = {
    "code": "VS Code",
    "devenv": "Visual Studio",
    "msedge": "Edge",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "windowsterminal": "Terminal",
    "powershell": "PowerShell",
    "pwsh": "PowerShell",
    "explorer": "File Explorer",
    "olk": "Outlook",
    "outlook": "Outlook",
    "ms-teams": "Teams",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "notepad": "Notepad",
    "obsidian": "Obsidian",
    "idea64": "IntelliJ IDEA",
    "pycharm64": "PyCharm",
}


def friendly_name(process_name: str) -> str:
    """What to call an application out loud.

    `Code.exe` read aloud as "code dot e x e" is the kind of detail that makes
    an assistant sound like a log file.
    """
    stem = process_name.rsplit(".", 1)[0] if "." in process_name else process_name
    return _FRIENDLY.get(stem.lower(), stem)


@dataclass(frozen=True, slots=True)
class Sample:
    """One observation. Mirrors the stored row, without the storage."""

    ts: datetime
    process_name: str
    is_idle: bool


@dataclass(frozen=True, slots=True)
class ActivityDigest:
    #: Seconds with the owner actually using the machine.
    active_s: float = 0.0
    #: Seconds at the machine but not touching it — screen on, nobody there.
    idle_s: float = 0.0
    #: The longest unbroken run of work, which is what a "you have been at this
    #: for two hours" warning is about.
    longest_stretch_s: float = 0.0
    #: How long the current run of work has lasted, if one is still going.
    current_stretch_s: float = 0.0
    #: Applications by time in the foreground while active, longest first.
    by_app: tuple[tuple[str, float], ...] = ()
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    @property
    def active_minutes(self) -> int:
        return round(self.active_s / 60)

    def as_result(self) -> dict[str, Any]:
        """Shaped for the model: minutes, not seconds, and few of them.

        Seconds invite an assistant to say "one hundred and forty-seven minutes"
        where a person says "about two and a half hours".
        """
        return {
            "active_minutes": self.active_minutes,
            "idle_minutes": round(self.idle_s / 60),
            "longest_stretch_minutes": round(self.longest_stretch_s / 60),
            "apps": [
                {"name": name, "minutes": round(seconds / 60)}
                for name, seconds in self.by_app[:_TOP_APPS]
                if seconds >= 60
            ],
            **({"since": self.first_seen.isoformat()} if self.first_seen else {}),
        }


def summarise(
    samples: Sequence[Sample] | Iterable[Sample],
    *,
    now: datetime,
    max_gap_s: float = _MAX_GAP_S,
    break_s: float = _BREAK_S,
) -> ActivityDigest:
    """Reduce a day of samples to the few numbers worth saying."""
    ordered = sorted(samples, key=lambda sample: sample.ts)
    if not ordered:
        return ActivityDigest()

    active = idle = 0.0
    longest = current = 0.0
    # How long the owner has been away *without interruption*. One idle sample
    # is a pause to read something; ten minutes of them is them having left, and
    # only the second should end a stretch of work.
    idle_run = 0.0
    by_app: dict[str, float] = {}

    for earlier, later in pairwise(ordered):
        gap = (later.ts - earlier.ts).total_seconds()
        if gap <= 0:
            # Two samples at the same instant. Duplicates happen — a batch
            # replayed after a reconnect, two samplers within the same second —
            # and they are nothing at all: no time passed, so no time is
            # counted, and nothing about the stretch has changed. An earlier
            # version reset the stretch here, which quietly chopped a long
            # afternoon into pieces and suppressed the warning that depends on
            # noticing one.
            continue
        if gap > max_gap_s:
            # Nothing was observed across this gap. Ending the current stretch
            # is the honest reading: the machine was not being watched, so it
            # cannot be claimed as continuous work.
            current = idle_run = 0.0
            continue

        if earlier.is_idle:
            idle += gap
            idle_run += gap
            if idle_run >= break_s:
                current = 0.0
        else:
            idle_run = 0.0
            active += gap
            current += gap
            longest = max(longest, current)
            name = friendly_name(earlier.process_name)
            by_app[name] = by_app.get(name, 0.0) + gap

    # The last sample has no successor, so its own span is unknown. Counting it
    # up to `now` is only fair while the sample is recent; past that the agent
    # has stopped reporting and time has not been observed.
    last = ordered[-1]
    trailing = (now - last.ts).total_seconds()
    if 0 < trailing <= max_gap_s and not last.is_idle:
        active += trailing
        current += trailing
        longest = max(longest, current)
        name = friendly_name(last.process_name)
        by_app[name] = by_app.get(name, 0.0) + trailing
    elif trailing > max_gap_s:
        # The agent has stopped reporting. Whatever the owner is doing now, this
        # process is not watching it, so there is no stretch in progress.
        current = 0.0

    return ActivityDigest(
        active_s=active,
        idle_s=idle,
        longest_stretch_s=longest,
        current_stretch_s=current,
        by_app=tuple(sorted(by_app.items(), key=lambda item: item[1], reverse=True)),
        first_seen=ordered[0].ts,
        last_seen=last.ts,
    )
