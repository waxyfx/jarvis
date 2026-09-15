"""Saying something the owner did not ask for.

Every other path in this backend starts with a person. This one starts with a
clock, which makes it the one place where getting it wrong is not a wrong answer
but an interruption — and an assistant that interrupts badly gets muted, after
which none of the rest of this matters.

Three things follow from that.

**It is signed.** A notification is not an action on the machine, but it is a
sentence spoken aloud in the owner's room in the assistant's voice. That is a
channel worth authenticating, and the signing machinery already exists.

**It is recorded.** Anything the assistant says on its own initiative goes into
the audit chain, so "why did it tell me that" has an answer.

**It is dropped, not queued, when the machine is offline.** A reminder about a
meeting that started forty minutes ago is not a reminder — it is noise with a
timestamp, and delivering a pile of them at reconnect is the single fastest way
to teach someone to ignore notifications.
"""

from __future__ import annotations

import uuid

from atlas_backend.audit import AuditActor, AuditEvent, append_detached
from atlas_backend.db.session import Database
from atlas_backend.logging import get_logger
from atlas_backend.server_identity import ServerIdentity
from atlas_backend.ws.hub import Hub
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.messages import Notify, build_envelope

__all__ = ["Notifier"]

log = get_logger(__name__)


class Notifier:
    """Delivers one notification to one device, or reports that it could not."""

    def __init__(
        self, *, hub: Hub, server_identity: ServerIdentity, database: Database | None = None
    ) -> None:
        self._hub = hub
        self._identity = server_identity
        self._database = database

    async def send(self, device_id: uuid.UUID, notification: Notify) -> bool:
        """Deliver it. Returns whether it reached a connected machine.

        Not raising when the machine is offline is deliberate: a scheduler that
        has to handle an exception every time a laptop is shut will grow a
        `try/except: pass`, and then a real failure will look the same as a
        closed lid.
        """
        if not self._hub.is_connected(device_id):
            log.info(
                "notification_undelivered",
                kind=notification.kind.value,
                reason="device offline",
            )
            await self._record(
                device_id, notification, AuditEvent.NOTIFICATION_SUPPRESSED, reason="offline"
            )
            return False

        envelope = self._identity.sign(
            build_envelope("server.notify", notification, corr_id=new_ulid())
        )
        delivered = await self._hub.send(device_id, envelope.to_json())

        if delivered:
            await self._record(device_id, notification, AuditEvent.NOTIFICATION_SENT)
            log.info(
                "notification_sent",
                kind=notification.kind.value,
                priority=notification.priority.value,
                spoken=notification.speak,
            )
        return delivered

    async def _record(
        self, device_id: uuid.UUID, notification: Notify, event: AuditEvent, **extra: object
    ) -> None:
        """Into the audit chain, so "why did it say that" has an answer.

        Not the body. This log is readable from the iPhone Settings screen and
        the rule here has always been identifiers and decisions rather than
        transcripts; the kind and the moment are enough to reconstruct which
        rule fired and why.

        Detached, in its own transaction: this runs from a scheduler rather than
        from a request, so there is no surrounding transaction to join, and a
        problem writing the trail must not take the scheduler down with it.
        """
        if self._database is None:
            return

        await append_detached(
            self._database,
            event_type=event,
            # There is already an actor for this: nothing here was asked for
            # by a person, and the trail should say so.
            actor=AuditActor.SCHEDULER,
            device_id=device_id,
            payload={
                "notification_id": notification.notification_id,
                "kind": notification.kind.value,
                "priority": notification.priority.value,
                "spoken": notification.speak,
                **extra,
            },
        )
