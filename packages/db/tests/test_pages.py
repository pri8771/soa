"""Page model tests (PRC-005): stable 1-based numbering, run scoping,
artifact references, tenant scope."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.pages import DocumentPageRepository, create_page
from soa_db.repository import OrganizationContext

ORG_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOC = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN_1 = uuid.UUID("55555555-5555-4555-8555-555555555555")
RUN_2 = uuid.UUID("66666666-6666-4666-8666-666666666666")
CONTEXT = OrganizationContext(organization_id=ORG_A)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/pages.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_pages_are_run_scoped_with_stable_numbering(db: DatabaseSessions) -> None:
    image_ids = [uuid.uuid4() for _ in range(3)]
    async with db.session_scope() as session:
        # Deliberately created out of order: listing is by page_number.
        for number, image_id in ((2, image_ids[1]), (1, image_ids[0])):
            await create_page(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN_1,
                page_number=number,
                width_px=400,
                height_px=200,
                dpi=144,
                image_artifact_id=image_id,
            )
        # A reprocess run gets its own page 1 without touching run 1's.
        await create_page(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN_2,
            page_number=1,
            width_px=800,
            height_px=400,
            dpi=300,
            image_artifact_id=image_ids[2],
        )

    async with db.session_scope() as session:
        run_one = await DocumentPageRepository(session, CONTEXT).list_for_run(RUN_1)
        assert [p.page_number for p in run_one] == [1, 2]
        assert run_one[0].image_artifact_id == image_ids[0]
        assert (run_one[0].width_px, run_one[0].height_px, run_one[0].dpi) == (400, 200, 144)
        # Text/layout artifacts are honestly absent until produced.
        assert run_one[0].text_artifact_id is None
        assert run_one[0].layout_artifact_id is None
        run_two = await DocumentPageRepository(session, CONTEXT).list_for_run(RUN_2)
        assert [p.page_number for p in run_two] == [1]


async def test_page_numbers_are_unique_per_run_and_one_based(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await create_page(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN_1,
            page_number=1,
            width_px=10,
            height_px=10,
            dpi=None,
            image_artifact_id=uuid.uuid4(),
        )
        with pytest.raises(ValueError, match="1-based"):
            await create_page(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN_1,
                page_number=0,
                width_px=10,
                height_px=10,
                dpi=None,
                image_artifact_id=uuid.uuid4(),
            )
    with pytest.raises(IntegrityError):
        async with db.session_scope() as session:
            await create_page(
                session,
                CONTEXT,
                document_id=DOC,
                run_id=RUN_1,
                page_number=1,  # duplicate within the run
                width_px=10,
                height_px=10,
                dpi=None,
                image_artifact_id=uuid.uuid4(),
            )


async def test_pages_are_tenant_scoped(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        await create_page(
            session,
            CONTEXT,
            document_id=DOC,
            run_id=RUN_1,
            page_number=1,
            width_px=10,
            height_px=10,
            dpi=None,
            image_artifact_id=uuid.uuid4(),
        )
    async with db.session_scope() as session:
        other = OrganizationContext(organization_id=ORG_B)
        assert await DocumentPageRepository(session, other).list_for_run(RUN_1) == []

    from soa_db.tenant_guard import RLS_PROTECTED_TABLES

    assert "document_pages" in RLS_PROTECTED_TABLES
