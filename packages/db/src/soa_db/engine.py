"""Engine construction and session lifecycle.

Sessions come from ``DatabaseSessions.session_scope()`` — an async context
manager that commits on success, rolls back on error, and always closes.
Nothing here runs migrations: applying migrations is an explicit operator
action (``make migrate``), never an application-startup side effect, so web
replicas cannot race each other on schema changes.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

RollbackAction = Callable[[], Awaitable[None]]
_ROLLBACK_ACTIONS_KEY = "soa.rollback_actions"


def register_rollback_action(session: AsyncSession, action: RollbackAction) -> None:
    """Register external compensation for this database unit of work.

    Object stores and secret managers cannot participate in the SQL transaction.
    Callers register an idempotent async action immediately after creating an
    external resource.  A failed flush or commit then removes that resource;
    a successful commit discards the action.
    """

    actions = session.info.setdefault(_ROLLBACK_ACTIONS_KEY, [])
    actions.append(action)


async def commit_unit_of_work(session: AsyncSession) -> None:
    """Commit an intentional mid-handler boundary and seal compensations.

    Long-running workers sometimes persist an external object plus its
    metadata before beginning a separate network call. A successful explicit
    commit makes those rollback actions obsolete; a failed commit leaves them
    registered so the surrounding ``session_scope`` can compensate.
    """

    await session.commit()
    session.info.pop(_ROLLBACK_ACTIONS_KEY, None)


async def _run_rollback_actions(session: AsyncSession) -> None:
    actions = session.info.pop(_ROLLBACK_ACTIONS_KEY, [])
    for action in reversed(actions):
        try:
            await action()
        except Exception:
            # Preserve the original database/application error. The failed
            # compensation is still visible to operations and can be retried
            # by external-resource reconciliation.
            logger.exception("external rollback compensation failed")


def create_database_engine(
    database_url: SecretStr | str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 5,
) -> AsyncEngine:
    url = database_url.get_secret_value() if isinstance(database_url, SecretStr) else database_url
    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
    # SQLite is used for isolated tests/local smoke runs. Do not retain its
    # aiosqlite worker threads across event-loop lifetimes: an un-disposed
    # pooled connection can otherwise try to signal a loop pytest has
    # already closed. PostgreSQL keeps the bounded production pool.
    if url.startswith("sqlite"):
        kwargs["poolclass"] = NullPool
    else:
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
            try:
                await session.rollback()
            finally:
                await _run_rollback_actions(session)
            raise
        else:
            session.info.pop(_ROLLBACK_ACTIONS_KEY, None)
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
