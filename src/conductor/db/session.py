from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert


def dialect_insert(session: AsyncSession) -> Callable[..., Insert]:
    """INSERT constructor whose ON CONFLICT matches the session dialect."""
    bind = session.get_bind()
    name = getattr(getattr(bind, "dialect", None), "name", "sqlite")
    return pg_insert if name == "postgresql" else sqlite_insert


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
