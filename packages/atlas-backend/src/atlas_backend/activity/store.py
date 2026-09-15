"""Reading activity samples back out of the database.

Separated from the arithmetic next door so the arithmetic can be tested without
a database, which is most of why it has any tests at all.

The queries here are always bounded by time and by device. An unbounded read of
this table is a day's worth of rows for one question, and the answer only ever
needs a window of it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.activity.summary import Sample
from atlas_backend.db.models import ActivitySampleRow

__all__ = ["samples_between", "start_of_day"]


def start_of_day(now: datetime) -> datetime:
    """Midnight before ``now``, in whatever timezone ``now`` carries.

    "Today" is the owner's day, not UTC's. The caller supplies a moment in the
    zone that matters, and this keeps that zone.
    """
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def samples_between(
    session: AsyncSession,
    *,
    device_id: uuid.UUID,
    since: datetime,
    until: datetime | None = None,
) -> list[Sample]:
    query = (
        select(
            ActivitySampleRow.ts,
            ActivitySampleRow.process_name,
            ActivitySampleRow.is_idle,
        )
        .where(ActivitySampleRow.device_id == device_id, ActivitySampleRow.ts >= since)
        .order_by(ActivitySampleRow.ts)
    )
    if until is not None:
        query = query.where(ActivitySampleRow.ts <= until)

    rows = await session.execute(query)
    return [Sample(ts=ts, process_name=name, is_idle=idle) for ts, name, idle in rows.all()]
