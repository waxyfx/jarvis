"""Reading and verifying the audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from atlas_backend.api.schemas import AuditEntryOut
from atlas_backend.audit import verify_chain
from atlas_backend.auth.deps import DbSession, TrustedDevice
from atlas_backend.db.models import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditEntryOut])
async def list_entries(
    session: DbSession,
    _caller: TrustedDevice,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    event_type: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[datetime | None, Query()] = None,
) -> list[AuditEntryOut]:
    """Newest entries first, paged by ``before_seq``."""
    statement = select(AuditLog).order_by(AuditLog.seq.desc()).limit(limit)
    if before_seq is not None:
        statement = statement.where(AuditLog.seq < before_seq)
    if event_type is not None:
        statement = statement.where(AuditLog.event_type == event_type)
    if since is not None:
        statement = statement.where(AuditLog.ts >= since)

    rows = (await session.execute(statement)).scalars().all()
    return [AuditEntryOut.model_validate(row) for row in rows]


@router.post("/verify")
async def verify(session: DbSession, _caller: TrustedDevice) -> dict[str, object]:
    """Recompute the whole hash chain and report the first inconsistency.

    A failure here means the log was modified outside the append path — treat it
    as a security incident, not a bug to paper over.
    """
    result = await verify_chain(session)
    return {
        "ok": result.ok,
        "entries_checked": result.entries_checked,
        "first_bad_seq": result.first_bad_seq,
        "reason": result.reason,
    }
