from pathlib import Path

import pytest
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, DatabaseSessions, create_database_engine, register_rollback_action


class Parent(Base):
    __tablename__ = "test_parent"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    __table_args__ = (UniqueConstraint("name"),)


class Child(Base):
    __tablename__ = "test_child"
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("test_parent.id"))


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_session_scope_commits_on_success(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        session.add(Parent(name="committed"))
    async with sessions.session_scope() as session:
        found = (await session.execute(select(Parent).where(Parent.name == "committed"))).all()
    assert len(found) == 1
    await sessions.dispose()


async def test_session_scope_rolls_back_on_error(sessions: DatabaseSessions) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with sessions.session_scope() as session:
            session.add(Parent(name="rolled-back"))
            raise RuntimeError("boom")
    async with sessions.session_scope() as session:
        found = (await session.execute(select(Parent).where(Parent.name == "rolled-back"))).all()
    assert found == []
    await sessions.dispose()


async def test_session_scope_compensates_external_writes_only_on_rollback(
    sessions: DatabaseSessions,
) -> None:
    calls: list[str] = []
    async with sessions.session_scope() as session:
        register_rollback_action(session, lambda: _record(calls, "committed"))
        session.add(Parent(name="kept"))
    assert calls == []

    with pytest.raises(RuntimeError, match="boom"):
        async with sessions.session_scope() as session:
            register_rollback_action(session, lambda: _record(calls, "first"))
            register_rollback_action(session, lambda: _record(calls, "second"))
            session.add(Parent(name="discarded"))
            raise RuntimeError("boom")
    assert calls == ["second", "first"]
    await sessions.dispose()


async def _record(calls: list[str], value: str) -> None:
    calls.append(value)


async def test_ping_healthy(sessions: DatabaseSessions) -> None:
    assert await sessions.ping() is True
    await sessions.dispose()


async def test_ping_reports_unreachable_database() -> None:
    engine = create_database_engine("sqlite+aiosqlite:////nonexistent-dir/nope/db.sqlite")
    sessions = DatabaseSessions(engine)
    assert await sessions.ping() is False
    await sessions.dispose()


def test_naming_conventions_produce_deterministic_names() -> None:
    parent = Parent.__table__
    child = Child.__table__
    constraint_names = {c.name for c in parent.constraints} | {c.name for c in child.constraints}
    assert "pk_test_parent" in constraint_names
    assert "uq_test_parent_name" in constraint_names
    assert "fk_test_child_parent_id_test_parent" in constraint_names
