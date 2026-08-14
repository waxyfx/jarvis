"""Persistent model for M1: identity, pairing, sessions and the audit chain.

Later phases add their own tables alongside these, each with its own migration.
Nothing here is speculative — every column is written or read by M1 code.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from atlas_backend.db.base import Base, utc_now
from atlas_shared.enums import DeviceKind, TrustLevel

__all__ = [
    "ActivitySampleRow",
    "ApiUsageRow",
    "AuditLog",
    "AuthChallenge",
    "Conversation",
    "Device",
    "DeviceSession",
    "Message",
    "PairingCode",
    "PermissionOverrideRow",
    "SystemTelemetryRow",
    "ToolCall",
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


class ToolCall(Base):
    """One request to run a tool, from policy decision to final outcome.

    Written before anything is dispatched, so a command that is denied — or that
    never comes back — is as visible as one that succeeded.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_device_id_created_at", "device_id", "created_at"),
        Index("ix_tool_calls_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    requested_by_device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)

    tool_name: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[int] = mapped_column(Integer)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    #: What the server's policy computed.
    risk_assessed: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(16))
    #: Human-readable rule reasons, in the order they applied.
    policy_reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)

    status: Mapped[str] = mapped_column(String(24))
    #: The assistant turn that produced this call, when a model was involved.
    message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    confirmed_by_device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    #: What the *agent* independently computed. A divergence is a signal, not a
    #: formality, so it is stored rather than discarded.
    risk_local: Mapped[str | None] = mapped_column(String(16), default=None)
    refusal: Mapped[str | None] = mapped_column(String(32), default=None)

    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PermissionOverrideRow(Base):
    """A standing user decision that adjusts the default policy for a tool.

    Overrides may make policy *stricter* freely. They may only relax it for LOW
    and MEDIUM risk — HIGH always requires a fresh confirmation, and DENY is
    never negotiable. That rule lives in the engine, not here.
    """

    __tablename__ = "permissions"
    __table_args__ = (
        CheckConstraint("mode IN ('always_allow', 'always_confirm', 'deny')", name="mode_known"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    #: Exact tool name, or a prefix pattern ending in ``.*`` (e.g. ``fs.*``).
    tool_pattern: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Conversation(Base):
    """A run of assistant turns. One open conversation per device at a time."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_id_started_at", "user_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    origin_device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    language: Mapped[str] = mapped_column(String(8), default="ru")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Message(Base):
    """One side of one exchange.

    ``expires_at`` exists so transcripts can be aged out on a retention policy;
    nothing prunes them yet (M10).
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8), default=None)
    input_modality: Mapped[str] = mapped_column(String(16), default="text")
    llm_model: Mapped[str | None] = mapped_column(String(64), default=None)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    stopped_because: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ApiUsageRow(Base):
    """Daily token accounting, so a runaway loop cannot run up a bill unseen."""

    __tablename__ = "api_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)


class ActivitySampleRow(Base):
    """Foreground application and idle state.

    There is deliberately **no column for window titles, keystrokes or clipboard
    contents**. The schema is the enforcement: a future change that wanted to
    store them would have to be a visible migration, not a quiet code edit.
    """

    __tablename__ = "activity_samples"
    __table_args__ = (Index("ix_activity_samples_ts", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    process_name: Mapped[str] = mapped_column(String(128))
    is_idle: Mapped[bool] = mapped_column(Boolean)
    idle_seconds: Mapped[int] = mapped_column(Integer)


class SystemTelemetryRow(Base):
    __tablename__ = "system_telemetry"
    __table_args__ = (Index("ix_system_telemetry_ts", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cpu_pct: Mapped[float] = mapped_column(Float)
    ram_used_pct: Mapped[float] = mapped_column(Float)
    ram_total_mb: Mapped[int] = mapped_column(Integer)
    disks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    uptime_s: Mapped[int] = mapped_column(BigInteger)
    gpu_temp_c: Mapped[float | None] = mapped_column(Float, default=None)


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
