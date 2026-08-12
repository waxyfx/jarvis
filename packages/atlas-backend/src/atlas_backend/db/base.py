"""Declarative base and shared column conventions."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

__all__ = ["Base", "utc_now"]

#: Explicit constraint naming, so Alembic autogenerate produces stable,
#: reversible migrations instead of database-assigned random names.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """Timezone-aware current time.

    Timestamps are generated in Python rather than by the database because the
    audit chain hashes the exact value it stores; a server-side ``now()`` would
    be invisible to the hashing code.
    """
    return datetime.now(UTC)
