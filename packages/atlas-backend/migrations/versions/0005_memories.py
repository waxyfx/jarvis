"""Facts the owner asked JARVIS to remember.

Revision ID: 0005_memories
Revises: 0004_reminders
Created: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_memories"
down_revision: str | None = "0004_reminders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        # Soft delete: a misheard "forget that" must not destroy something the
        # owner deliberately stored.
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memories_user", "memories", ["user_id", "forgotten_at"])


def downgrade() -> None:
    op.drop_index("ix_memories_user", table_name="memories")
    op.drop_table("memories")
