"""Sunny, over HTTP, as the only thing that knows it is Sunny.

Sunny is the owner's Life OS — Next.js and Prisma, deployed on Vercel. It was
already expecting this: `src/server/auth.ts` carries a machine-access path
written for JARVIS by name, enabled only when a token of at least 24 characters
is configured, compared in constant time, bound to one account rather than
falling back to whichever user the database returns first, and never logged.
None of that needed designing again, and a second authentication path would only
have been a second thing to get wrong.

**There is no method here that takes a URL.** Each action maps to one request,
and the mapping is written down rather than assembled. A model proposes
`tracker.complete_task` with a validated task id; it cannot propose a path, a
verb, a header or a body, because no function exists that would accept one.

**The token stays on the backend.** Not the Windows Agent, not the phone. It is
a credential for a remote service with write access to the owner's data, which
is the same class of thing as the Gemini key and lives under the same rule.

Absent configuration the tracker is simply unavailable: the tools do not reach
the catalogue and nothing here is constructed. Off by default, exactly as
Sunny's own side is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx

from atlas_backend.logging import get_logger
from atlas_backend.tracker.provider import (
    Applied,
    Goal,
    Habit,
    Priority,
    Task,
    TrackerError,
    TrackerUnavailableError,
)

__all__ = ["SunnyTracker"]

log = get_logger(__name__)

#: Sunny's own vocabulary for which slice of the list to return. Taken from its
#: `taskQuerySchema`, not invented: today | week | overdue | upcoming | unscheduled.
_SCOPE_TODAY = "today"
_SCOPE_UPCOMING = "upcoming"


def _as_datetime(value: Any) -> datetime | None:
    """Parse whatever Sunny put in a date field, or give up quietly.

    A tracker that answers with an unexpected shape should cost one missing
    deadline, not an exception halfway through reading the day aloud.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_priority(value: Any) -> Priority:
    try:
        return Priority(str(value))
    except ValueError:
        return Priority.MEDIUM


def _task_from(row: dict[str, Any]) -> Task:
    """One Sunny task, reduced to what can be spoken.

    Field names checked against the real deployment rather than assumed. Sunny
    keeps the scheduled day in `date` and the hour beside it in `startTime`, and
    `deadline` is a separate due-by that most tasks leave empty. An earlier
    version read `deadline` as the scheduled time and looked for a field called
    `time` that does not exist, so appointments arrived with no hour and
    anything scheduled earlier in the evening was reported as late.

    Everything else a task carries — subtasks, comments, attachments, tags,
    order, recurrence — is dropped here rather than downstream, because
    anything surviving this function is something the model eventually reads.
    """
    when = _as_datetime(row.get("date"))
    at = row.get("startTime") if isinstance(row.get("startTime"), str) else None
    if at is None and when is not None and (when.hour or when.minute):
        at = when.strftime("%H:%M")

    # `date` is midnight and the hour lives beside it; put them together so
    # "what is next" can compare moments rather than days.
    if when is not None and at:
        hour, _, minute = at.partition(":")
        if hour.isdigit() and minute.isdigit():
            when = when.replace(hour=int(hour), minute=int(minute))

    project = row.get("project")
    return Task(
        id=str(row.get("id", "")),
        title=str(row.get("title", "")).strip(),
        priority=_as_priority(row.get("priority")),
        when=when,
        at=at,
        due=_as_datetime(row.get("deadline")),
        done=bool(row.get("completedAt") or row.get("status") == "done"),
        project=str(project.get("name")) if isinstance(project, dict) else None,
    )


def _scheduling(moment: datetime) -> dict[str, Any]:
    """The two fields Sunny wants for "put this here".

    `date` alone leaves a task on a day with no hour, which is what the tracker
    means by unscheduled-within-the-day; sending both is how an appointment is
    made.
    """
    return {"date": moment.date().isoformat(), "startTime": moment.strftime("%H:%M")}


class SunnyTracker:
    """The tracker, reached over HTTP with a machine token."""

    name = "sunny"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise TrackerUnavailableError("no tracker URL is configured")
        if len(token) < 24:
            # Sunny refuses anything shorter on its own side; failing here says
            # so while the person can still read the message.
            raise TrackerUnavailableError("the tracker token is too short to be one")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_s
        self._transport = transport

    # ------------------------------------------------------------- plumbing

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """The only place a request is made. Callers pass a fixed path.

        Private on purpose. Nothing outside this class may reach it, and nothing
        inside it derives ``path`` from anything a model produced.
        """
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.TimeoutException as exc:
            raise TrackerError("the tracker did not answer in time") from exc
        except httpx.HTTPError as exc:
            # type(exc).__name__ rather than str(exc): httpx puts the URL in the
            # message, and the URL is where the token would end up if anything
            # ever moved it to a query parameter.
            raise TrackerError(f"could not reach the tracker: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            raise TrackerUnavailableError("the tracker refused the token")
        if response.status_code >= 400:
            raise TrackerError(f"the tracker answered {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise TrackerError("the tracker answered with something that is not JSON") from exc

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        """Sunny returns a list, or an object wrapping one. Accept both."""
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("items", "tasks", "goals", "habits", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
        return []

    # -------------------------------------------------------------- reading

    async def tasks_today(self) -> list[Task]:
        payload = await self._request("GET", "/api/tasks", params={"scope": _SCOPE_TODAY})
        return [_task_from(row) for row in self._rows(payload)]

    async def tasks_upcoming(self, *, days: int = 7) -> list[Task]:
        payload = await self._request(
            "GET", "/api/tasks", params={"scope": _SCOPE_UPCOMING, "limit": 200}
        )
        tasks = [_task_from(row) for row in self._rows(payload)]
        if days <= 0:
            return tasks
        horizon = datetime.now(UTC).timestamp() + days * 86400
        return [task for task in tasks if task.when is None or task.when.timestamp() <= horizon]

    async def goals(self) -> list[Goal]:
        payload = await self._request("GET", "/api/goals")
        goals: list[Goal] = []
        for row in self._rows(payload):
            target = _as_datetime(row.get("targetDate") or row.get("deadline"))
            goals.append(
                Goal(
                    id=str(row.get("id", "")),
                    title=str(row.get("title", "")).strip(),
                    progress=int(row.get("progress") or 0),
                    target_date=target.date() if target else None,
                )
            )
        return goals

    async def habits(self) -> list[Habit]:
        payload = await self._request("GET", "/api/habits")
        habits: list[Habit] = []
        for row in self._rows(payload):
            habits.append(
                Habit(
                    id=str(row.get("id", "")),
                    title=str(row.get("title", "")).strip(),
                    done_today=bool(row.get("doneToday") or row.get("completedToday")),
                    streak=int(row.get("streak") or 0),
                )
            )
        return habits

    # -------------------------------------------------------------- writing

    async def add_task(
        self,
        *,
        title: str,
        priority: Priority = Priority.MEDIUM,
        when: datetime | None = None,
    ) -> Applied:
        body: dict[str, Any] = {"title": title, "priority": priority.value}
        if when is not None:
            body.update(_scheduling(when))
        created = await self._request("POST", "/api/tasks", json=body)
        return Applied(what=title, detail="added", extra={"id": _id_of(created)})

    async def add_goal(self, *, title: str, target_date: date | None = None) -> Applied:
        body: dict[str, Any] = {"title": title}
        if target_date is not None:
            body["targetDate"] = target_date.isoformat()
        created = await self._request("POST", "/api/goals", json=body)
        return Applied(what=title, detail="added", extra={"id": _id_of(created)})

    async def complete_task(self, *, task_id: str) -> Applied:
        # Sunny has a dedicated toggle; using it keeps whatever bookkeeping it
        # does around completion — streaks, recurrence — rather than reaching
        # past it to set a field.
        updated = await self._request("POST", f"/api/tasks/{task_id}/toggle")
        return Applied(what=_title_of(updated) or task_id, detail="completed")

    async def reschedule_task(self, *, task_id: str, when: datetime) -> Applied:
        # The scheduled day and hour, not the deadline: "move it to tomorrow at
        # nine" is about where the task sits, and writing a deadline instead
        # leaves it on whatever day it was already on.
        updated = await self._request("PATCH", f"/api/tasks/{task_id}", json=_scheduling(when))
        return Applied(
            what=_title_of(updated) or task_id,
            detail=f"moved to {when.strftime('%Y-%m-%d %H:%M')}",
        )

    async def set_priority(self, *, task_id: str, priority: Priority) -> Applied:
        updated = await self._request(
            "PATCH", f"/api/tasks/{task_id}", json={"priority": priority.value}
        )
        return Applied(what=_title_of(updated) or task_id, detail=f"priority {priority.value}")


def _id_of(payload: Any) -> str:
    return str(payload.get("id", "")) if isinstance(payload, dict) else ""


def _title_of(payload: Any) -> str:
    """The title of whatever came back, so a reply can name it.

    "Done" is not an answer when the question was *which* task, and a misheard
    title that got this far has to be visible in what the owner hears.
    """
    return str(payload.get("title", "")).strip() if isinstance(payload, dict) else ""
