"""Persistent model for M1: identity, pairing, sessions and the audit chain.

Later phases add their own tables alongside these, each with its own migration.
Nothing here is speculative — every column is written or read by M1 code.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from atlas_backend.db.base import Base, utc_now
from atlas_shared.enums import DeviceKind, TrustLevel

__all__ = [
    "AuditLog",
    "AuthChallenge",
    "Device",
    "DeviceSession",
    "PairingCode",
    "User",
]


def _values(enum_type: type[StrEnum]) -> str:
    """Render an enum's values as a SQL ``IN`` list for a check constraint."""
    return ", ".join(f"'{member.value}'" for member in enum_type)


class User(Base):
    """The owner. Single-user today; the column exists so that never has to change."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(120))
    primary_lang: Mapped[str] = mapped_column(String(8), default="ru")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Almaty")
    quiet_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=lambda: {"start": "23:00", "end": "08:00"}
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    devices: Mapped[list[Device]] = relationship(back_populates="user")


class Device(Base):
    """A paired device, identified by an Ed25519 public key.

    The private half never reaches this server, so a dump of this table cannot
    be used to impersonate a device.
    """

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_values(DeviceKind)})", name="kind_known"),
        CheckConstraint(f"trust_level IN ({_values(TrustLevel)})", name="trust_level_known"),
        Index("ix_devices_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    #: Raw 32-byte Ed25519 key. Unique, so one key can never back two devices.
    public_key: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    trust_level: Mapped[str] = mapped_column(String(16), default=TrustLevel.TRUSTED.value)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    user: Mapped[User] = relationship(back_populates="devices")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.trust_level != TrustLevel.REVOKED.value


class PairingCode(Base):
    """A short-lived, single-use code that authorises one device to enrol.

    Only the hash is stored: a database dump does not reveal a live code.
    """

    __tablename__ = "pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    intended_kind: Mapped[str] = mapped_column(String(32))
    intended_name: Mapped[str] = mapped_column(String(120))
    issued_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), default=None
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthChallenge(Base):
    """Server-issued nonce a device signs to prove key possession.

    Server-generated (rather than client-generated) so the device cannot choose
    what it signs, and single-use so a captured signature cannot be replayed.
    """

    __tablename__ = "auth_challenges"
    __table_args__ = (Index("ix_auth_challenges_expires_at", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    nonce: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeviceSession(Base):
    """One realtime connection lifetime."""

    __tablename__ = "device_sessions"
    __table_args__ = (Index("ix_device_sessions_device_id_started_at", "device_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    remote_addr: Mapped[str | None] = mapped_column(String(64), default=None)
    close_reason: Mapped[str | None] = mapped_column(String(120), default=None)
    #: Set when the peer completed a valid hello handshake.
    handshake_ok: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    """Append-only, hash-chained record of everything that matters.

    ``hash = SHA-256(prev_hash || canonical_json(entry))``. Rewriting any past
    row breaks every hash after it, so tampering is detectable even by someone
    with write access to the table. Deletion and update are additionally blocked
    at the database level by a trigger created in the migration.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_ts", "ts"),
        Index("ix_audit_log_event_type_ts", "event_type", "ts"),
        Index("ix_audit_log_device_id_ts", "device_id", "ts"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(32))
    #: Intentionally *not* a foreign key. An append-only record of what happened
    #: must outlive the rows it mentions; a cascading SET NULL would also be an
    #: UPDATE, which the immutability trigger rejects — deleting a device would
    #: fail rather than the log adapting to it.
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), default=None)
    hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    #: Position in the chain, independent of the database-assigned seq.
    chain_index: Mapped[int] = mapped_column(Integer, default=0)
