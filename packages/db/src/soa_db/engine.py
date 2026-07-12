"""Engine construction and session lifecycle.

Sessions come from ``DatabaseSessions.session_scope()`` — an async context
manager that commits on success, rolls back on error, and always closes.
Nothing here runs migrations: applying migrations is an explicit operator
action (``make migrate``), never an application-startup side effect, so web
replicas cannot race each other on schema changes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_database_engine(
    database_url: SecretStr | str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 5,
) -> AsyncEngine:
    url = database_url.get_secret_value() if isinstance(database_url, SecretStr) else database_url
    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
    # SQLite (tests) does not accept pool sizing arguments.
    if not url.startswith("sqlite"):
        kwargs["pool_size"] = pool_size
        kwargs["max_overflow"] = max_overflow
    return create_async_engine(url, **kwargs)


class DatabaseSessions:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        """One unit of work: commit on success, rollback on exception."""
        session = self._sessionmaker()
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def ping(self) -> bool:
        """Readiness-check hook: cheap connectivity probe."""
        from sqlalchemy import text

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self._engine.dispose()
