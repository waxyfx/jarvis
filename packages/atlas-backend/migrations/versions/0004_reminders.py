"""Ad-hoc reminders: "напомни мне через двадцать минут".

Revision ID: 0004_reminders
Revises: 0003_assistant
Created: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_reminders"
down_revision: str | None = "0003_assistant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # The scheduler's only query: what is due and not yet said.
    op.create_index("ix_reminders_due", "reminders", ["due_at", "delivered_at"])


def downgrade() -> None:
    op.drop_index("ix_reminders_due", table_name="reminders")
    op.drop_table("reminders")
