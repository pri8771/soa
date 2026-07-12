import uuid
from pathlib import Path

import pytest
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from soa_db import Base, CursorRequest, DatabaseSessions, create_database_engine
from soa_db.mixins import UuidPrimaryKeyMixin
from soa_db.repository import (
    OrganizationContext,
    OrganizationScopedMixin,
    ScopedRepository,
    TenantMismatchError,
)

ORG_A = OrganizationContext(organization_id=uuid.UUID(int=0xA))
ORG_B = OrganizationContext(organization_id=uuid.UUID(int=0xB))


class Widget(UuidPrimaryKeyMixin, OrganizationScopedMixin, Base):
    __tablename__ = "test_widget"
    name: Mapped[str] = mapped_column(String(50))


class WidgetRepository(ScopedRepository[Widget]):
    model = Widget


@pytest.fixture
async def sessions(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/repo.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def seed(sessions: DatabaseSessions) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessions.session_scope() as session:
        a = Widget(name="belongs-to-A", organization_id=ORG_A.organization_id)
        b = Widget(name="belongs-to-B", organization_id=ORG_B.organization_id)
        session.add_all([a, b])
    return a.id, b.id


async def test_get_cannot_cross_tenants(sessions: DatabaseSessions) -> None:
    a_id, b_id = await seed(sessions)
    async with sessions.session_scope() as session:
        repo_a = WidgetRepository(session, ORG_A)
        assert (await repo_a.get(a_id)) is not None
        assert (await repo_a.get(b_id)) is None, "cross-tenant read must return nothing"
    await sessions.dispose()


async def test_list_and_count_are_scoped(sessions: DatabaseSessions) -> None:
    await seed(sessions)
    async with sessions.session_scope() as session:
        repo_a = WidgetRepository(session, ORG_A)
        page = await repo_a.list_page(CursorRequest(limit=50))
        assert [w.name for w in page.items] == ["belongs-to-A"]
        assert await repo_a.count() == 1
    await sessions.dispose()


async def test_add_stamps_missing_organization(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        repo_a = WidgetRepository(session, ORG_A)
        widget = repo_a.add(Widget(name="stamped"))
    assert widget.organization_id == ORG_A.organization_id
    await sessions.dispose()


async def test_add_rejects_foreign_organization(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        repo_a = WidgetRepository(session, ORG_A)
        foreign = Widget(name="foreign", organization_id=ORG_B.organization_id)
        with pytest.raises(TenantMismatchError):
            repo_a.add(foreign)
        session.expunge_all()
    await sessions.dispose()


async def test_pagination_chains_within_scope(sessions: DatabaseSessions) -> None:
    async with sessions.session_scope() as session:
        for i in range(5):
            session.add(Widget(name=f"a-{i}", organization_id=ORG_A.organization_id))
        for i in range(3):
            session.add(Widget(name=f"b-{i}", organization_id=ORG_B.organization_id))
    async with sessions.session_scope() as session:
        repo_a = WidgetRepository(session, ORG_A)
        from soa_db import decode_cursor

        first = await repo_a.list_page(CursorRequest(limit=2))
        assert len(first.items) == 2 and first.has_more
        assert first.next_cursor is not None
        second = await repo_a.list_page(
            CursorRequest(limit=2, after=decode_cursor(first.next_cursor))
        )
        assert len(second.items) == 2 and second.has_more
        assert second.next_cursor is not None
        third = await repo_a.list_page(
            CursorRequest(limit=2, after=decode_cursor(second.next_cursor))
        )
        assert len(third.items) == 1 and not third.has_more
        names = {w.name for w in first.items + second.items + third.items}
        assert names == {f"a-{i}" for i in range(5)}, "pages must never leak other tenants"
    await sessions.dispose()


async def test_get_for_update_takes_explicit_lock(sessions: DatabaseSessions) -> None:
    a_id, _ = await seed(sessions)
    async with sessions.session_scope() as session:
        repo_a = WidgetRepository(session, ORG_A)
        locked = await repo_a.get(a_id, for_update=True)
        assert locked is not None
    await sessions.dispose()


def test_repository_without_tenant_column_fails_at_class_definition() -> None:
    class Unscoped(UuidPrimaryKeyMixin, Base):
        __tablename__ = "test_unscoped"

    with pytest.raises(TypeError, match="no organization_id column"):

        class BadRepository(ScopedRepository[Unscoped]):  # type: ignore[type-var]
            model = Unscoped


def test_repository_without_model_fails_at_class_definition() -> None:
    with pytest.raises(TypeError, match="must define a 'model'"):

        class NoModelRepository(ScopedRepository[Widget]):
            pass
