"""Async engine and session lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from atlas_backend.config import Settings

__all__ = ["Database"]


class Database:
    """Owns the engine and hands out sessions.

    Held on ``app.state`` rather than in a module global, so tests can create an
    isolated instance without monkeypatching.
    """

    def __init__(self, settings: Settings) -> None:
        pool_options: dict[str, object] = (
            {"poolclass": NullPool}
            if settings.database_use_null_pool
            else {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5}
        )
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url,
            echo=settings.database_echo,
            **pool_options,
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A session wrapped in one transaction, committed on clean exit."""
        async with self._sessionmaker() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
            await session.commit()

    async def dispose(self) -> None:
        await self._engine.dispose()
