"""The append-only, hash-chained audit log.

Every security-relevant fact lands here: pairings, token issuance, connections,
policy decisions, and — from M3 — every tool ATLAS runs and every use of cloud
vision. Two properties make it worth trusting:

* **Append-only.** A database trigger (see migration 0001) rejects UPDATE and
  DELETE, so even a bug cannot quietly rewrite history.
* **Chained.** Each row hashes the previous row's hash together with its own
  content. Altering row *n* invalidates every row after it, and
  :func:`verify_chain` finds exactly where.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from atlas_backend.db.base import utc_now
from atlas_backend.db.models import AuditLog
from atlas_backend.db.session import Database
from atlas_backend.logging import get_logger
from atlas_shared.canonical import canonical_json
from atlas_shared.protocol.envelope import format_timestamp

__all__ = [
    "AuditActor",
    "AuditEvent",
    "ChainVerification",
    "append",
    "append_detached",
    "compute_entry_hash",
    "verify_chain",
]

_log = get_logger(__name__)

#: Advisory lock id serialising appends, so two concurrent writers cannot read
#: the same chain head and fork the chain. Arbitrary but fixed.
_CHAIN_LOCK_ID = 0x4154_4C41


class AuditActor(StrEnum):
    USER = "user"
    DEVICE = "device"
    SYSTEM = "system"
    SCHEDULER = "scheduler"


class AuditEvent(StrEnum):
    """Event types written in M1. Later phases extend this."""

    PAIRING_STARTED = "pairing.started"
    PAIRING_COMPLETED = "pairing.completed"
    PAIRING_FAILED = "pairing.failed"
    # The suppressions below: these are event names, not credentials.
    TOKEN_CHALLENGE_ISSUED = "auth.challenge_issued"  # noqa: S105
    TOKEN_ISSUED = "auth.token_issued"  # noqa: S105
    TOKEN_REJECTED = "auth.token_rejected"  # noqa: S105
    DEVICE_REVOKED = "device.revoked"
    CONNECTION_OPENED = "connection.opened"
    CONNECTION_CLOSED = "connection.closed"
    CONNECTION_REJECTED = "connection.rejected"
    PROTOCOL_VIOLATION = "connection.protocol_violation"
    AGENT_MODE_CHANGED = "agent.mode_changed"

    # Tool execution (M2). Every outcome is recorded, including the ones where
    # nothing ran.
    TOOL_DENIED = "tool.denied"
    TOOL_CONFIRMATION_REQUIRED = "tool.confirmation_required"
    TOOL_CONFIRMED = "tool.confirmed"
    TOOL_DISPATCHED = "tool.dispatched"
    TOOL_EXECUTED = "tool.executed"
    TOOL_REFUSED = "tool.refused_by_agent"
    TOOL_FAILED = "tool.failed"
    #: A result arrived whose signature did not verify against the device key.
    TOOL_RESULT_UNVERIFIED = "tool.result_unverified"


def compute_entry_hash(
    *,
    prev_hash: bytes | None,
    chain_index: int,
    ts: datetime,
    actor: str,
    device_id: uuid.UUID | None,
    event_type: str,
    payload: dict[str, Any],
) -> bytes:
    """Hash one entry against its predecessor.

    ``chain_index`` is inside the hash so that removing a whole row — which
    would otherwise leave a self-consistent shorter chain — is also detectable.
    """
    body = canonical_json(
        {
            "chain_index": chain_index,
            "ts": format_timestamp(ts),
            "actor": actor,
            "device_id": str(device_id) if device_id else None,
            "event_type": event_type,
            "payload": payload,
        }
    )
    return hashlib.sha256((prev_hash or b"") + body).digest()


async def append(
    session: AsyncSession,
    *,
    actor: AuditActor,
    event_type: AuditEvent,
    payload: dict[str, Any] | None = None,
    device_id: uuid.UUID | None = None,
) -> AuditLog:
    """Append one entry. Must run inside a transaction.

    The caller is responsible for never passing secrets in ``payload``: this log
    is readable from the iPhone Settings screen. Pass identifiers and decisions,
    not credentials, screenshots or transcript bodies.
    """
    # Held until the surrounding transaction ends, which is what makes the
    # read-then-write of the chain head atomic.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _CHAIN_LOCK_ID}
    )

    head = (
        await session.execute(select(AuditLog).order_by(AuditLog.seq.desc()).limit(1))
    ).scalar_one_or_none()

    prev_hash = head.hash if head is not None else None
    chain_index = head.chain_index + 1 if head is not None else 0
    timestamp = utc_now()
    body = payload or {}

    entry = AuditLog(
        ts=timestamp,
        actor=actor.value,
        device_id=device_id,
        event_type=event_type.value,
        payload=body,
        prev_hash=prev_hash,
        chain_index=chain_index,
        hash=compute_entry_hash(
            prev_hash=prev_hash,
            chain_index=chain_index,
            ts=timestamp,
            actor=actor.value,
            device_id=device_id,
            event_type=event_type.value,
            payload=body,
        ),
    )
    session.add(entry)
    await session.flush()
    return entry


async def append_detached(
    database: Database,
    *,
    actor: AuditActor,
    event_type: AuditEvent,
    payload: dict[str, Any] | None = None,
    device_id: uuid.UUID | None = None,
) -> None:
    """Append an entry in its **own** transaction.

    Required whenever the event being recorded is a *failure*: the request's
    transaction is about to roll back, and an audit row written inside it would
    roll back with the very thing it was meant to record. Rejected pairings and
    refused tokens are exactly the entries most worth keeping.

    Never raises: a problem writing the audit trail must not replace the error
    the caller is already reporting.

    Must not be called while the caller's own transaction already holds the
    chain lock — that is, after a successful :func:`append` in the same request.
    Doing so would wait on a lock only the caller can release.
    """
    try:
        async with database.transaction() as session:
            await append(
                session,
                actor=actor,
                event_type=event_type,
                payload=payload,
                device_id=device_id,
            )
    except Exception:
        _log.exception("audit_append_failed", event_type=event_type.value)


@dataclass(frozen=True, slots=True)
class ChainVerification:
    ok: bool
    entries_checked: int
    first_bad_seq: int | None = None
    reason: str | None = None


async def verify_chain(session: AsyncSession) -> ChainVerification:
    """Recompute the whole chain and report the first inconsistency.

    Cheap enough to run on startup and from the runbook; if it ever fails, the
    log has been tampered with or a write path bypassed :func:`append`.
    """
    rows = (await session.execute(select(AuditLog).order_by(AuditLog.seq.asc()))).scalars().all()

    expected_prev: bytes | None = None
    for position, row in enumerate(rows):
        if row.chain_index != position:
            return ChainVerification(
                ok=False,
                entries_checked=position,
                first_bad_seq=row.seq,
                reason=f"chain_index {row.chain_index} at position {position} (row missing?)",
            )
        if row.prev_hash != expected_prev:
            return ChainVerification(
                ok=False,
                entries_checked=position,
                first_bad_seq=row.seq,
                reason="prev_hash does not match the preceding entry",
            )
        recomputed = compute_entry_hash(
            prev_hash=row.prev_hash,
            chain_index=row.chain_index,
            ts=row.ts,
            actor=row.actor,
            device_id=row.device_id,
            event_type=row.event_type,
            payload=row.payload,
        )
        if recomputed != row.hash:
            return ChainVerification(
                ok=False,
                entries_checked=position,
                first_bad_seq=row.seq,
                reason="content does not match the stored hash",
            )
        expected_prev = row.hash

    return ChainVerification(ok=True, entries_checked=len(rows))
