"""What the owner has actually been doing at the machine.

The agent reports foreground process and idle state as metadata only — no
window titles, no keystrokes, no clipboard, and the database schema is the
enforcement rather than a promise. This package is the reading half: the
arithmetic that turns samples into hours, and the one tool that says them.
"""

from atlas_backend.activity.summary import ActivityDigest, Sample, friendly_name, summarise
from atlas_backend.activity.tools import ACTIVITY_TOOLS, ActivityToolError, run_activity_tool

__all__ = [
    "ACTIVITY_TOOLS",
    "ActivityDigest",
    "ActivityToolError",
    "Sample",
    "friendly_name",
    "run_activity_tool",
    "summarise",
]
