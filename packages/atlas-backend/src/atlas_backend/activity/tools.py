"""The tool behind "сколько я сегодня работал?".

It runs on the backend rather than the agent, even though the data is about the
agent's machine, because the backend is where the samples are stored. Asking the
laptop would mean asking it to remember its own day, which it does not.

One tool, and deliberately one: the useful question is "today", and every
variation of it — this week, last Tuesday, per-application breakdowns — is a
report rather than a thing to say out loud. Those belong in the daily report,
not in a voice answer.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.activity.store import samples_between, start_of_day
from atlas_backend.activity.summary import summarise

__all__ = ["ACTIVITY_TOOLS", "ActivityToolError", "run_activity_tool"]


class ActivityToolError(RuntimeError):
    """Something to report, rather than something to crash on."""


async def _today(
    session: AsyncSession, device_id: uuid.UUID, args: Mapping[str, Any]
) -> dict[str, Any]:
    now = datetime.now(UTC)
    hours = args.get("hours")
    since = now - timedelta(hours=float(hours)) if hours else start_of_day(now)

    digest = summarise(await samples_between(session, device_id=device_id, since=since), now=now)
    result = digest.as_result()
    if hours:
        result["window_hours"] = int(hours)
    return result


#: The whole surface. See the module docstring for why it is one entry.
ACTIVITY_TOOLS = {"activity.today": _today}


async def run_activity_tool(
    session: AsyncSession, device_id: uuid.UUID, name: str, args: Mapping[str, Any]
) -> dict[str, Any]:
    handler = ACTIVITY_TOOLS.get(name)
    if handler is None:
        raise ActivityToolError(f"{name} is not something I can tell you about")
    return await handler(session, device_id, args)
