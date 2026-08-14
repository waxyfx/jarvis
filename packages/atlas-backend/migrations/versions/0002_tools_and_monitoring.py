"""M2: tool calls, permission overrides, activity and telemetry.

Revision ID: 0002_tools
Revises: 0001_initial
Created: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_tools"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No foreign key on device_id, for the same reason as audit_log: the record
    # of what was attempted must outlive the device row it refers to.
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_device_id", sa.Uuid(), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_assessed", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("policy_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("confirmed_by_device_id", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("risk_local", sa.String(length=16), nullable=True),
        sa.Column("refusal", sa.String(length=32), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    op.create_index("ix_tool_calls_device_id_created_at", "tool_calls", ["device_id", "created_at"])
    op.create_index("ix_tool_calls_status", "tool_calls", ["status"])

    op.create_table(
        "permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tool_pattern", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('always_allow', 'always_confirm', 'deny')",
            name=op.f("ck_permissions_mode_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_permissions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
    )

    # Metadata only. There is deliberately no column for window titles,
    # keystrokes or clipboard contents: adding one would have to be a visible
    # migration, not a quiet change in the collector.
    op.create_table(
        "activity_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("process_name", sa.String(length=128), nullable=False),
        sa.Column("is_idle", sa.Boolean(), nullable=False),
        sa.Column("idle_seconds", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_samples")),
    )
    op.create_index("ix_activity_samples_ts", "activity_samples", ["ts"])

    op.create_table(
        "system_telemetry",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_pct", sa.Float(), nullable=False),
        sa.Column("ram_used_pct", sa.Float(), nullable=False),
        sa.Column("ram_total_mb", sa.Integer(), nullable=False),
        sa.Column("disks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uptime_s", sa.BigInteger(), nullable=False),
        sa.Column("gpu_temp_c", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_telemetry")),
    )
    op.create_index("ix_system_telemetry_ts", "system_telemetry", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_system_telemetry_ts", table_name="system_telemetry")
    op.drop_table("system_telemetry")
    op.drop_index("ix_activity_samples_ts", table_name="activity_samples")
    op.drop_table("activity_samples")
    op.drop_table("permissions")
    op.drop_index("ix_tool_calls_status", table_name="tool_calls")
    op.drop_index("ix_tool_calls_device_id_created_at", table_name="tool_calls")
    op.drop_table("tool_calls")
