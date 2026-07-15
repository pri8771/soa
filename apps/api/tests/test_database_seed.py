from pathlib import Path

import pytest
from sqlalchemy import func, select

from soa_api.domain.tenancy import Organization
from soa_api.ops.seed import seed_database
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import Document


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/seed.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    database = DatabaseSessions(engine)
    yield database
    await database.dispose()


async def test_database_seed_is_idempotent(db) -> None:
    first = await seed_database(db)
    second = await seed_database(db)

    assert sum(first.created.values()) > 0
    assert sum(second.created.values()) == 0
    async with db.session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(Organization)) == 1
        assert await session.scalar(select(func.count()).select_from(Document)) == 5
