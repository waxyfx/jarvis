"""The day, written down.

Separate from :mod:`atlas_backend.notify` because they answer different
questions. A notification is for someone who is here now and needs to know
something in the next few minutes; a report is for someone looking back at the
week. The evening summary says "three hours twenty"; this says which three
hours, on what, and which tasks are still open.
"""

from atlas_backend.reports.daily import DayReport, NoteSink, build_report
from atlas_backend.reports.writer import DailyReportWriter

__all__ = ["DailyReportWriter", "DayReport", "NoteSink", "build_report"]
