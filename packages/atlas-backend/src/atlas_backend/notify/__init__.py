"""The half of the assistant that speaks first.

Everything else here runs because the owner said something. This package runs
because of a clock: a reminder before a meeting, a briefing in the morning, a
word about having been at the desk for three hours.

Three parts, deliberately separate. :mod:`rules` decides *whether* there is
anything worth saying and works on a snapshot, so it can be tested against a
made-up Tuesday afternoon. :mod:`notifier` delivers one, signed and recorded.
:mod:`scheduler` is the clock, and is the only part that can fail without
anyone noticing — which is why it logs every failure and never stops.
"""

from atlas_backend.notify.notifier import Notifier
from atlas_backend.notify.rules import Moment, Planned, Schedule, decide_all
from atlas_backend.notify.scheduler import ProactiveScheduler

__all__ = [
    "Moment",
    "Notifier",
    "Planned",
    "ProactiveScheduler",
    "Schedule",
    "decide_all",
]
