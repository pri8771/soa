"""Real-PostgreSQL regression test for the SSE push channel (JOB-007).

SQLite stores the GUID primary key as text, so ``MAX()`` over it works by
accident; PostgreSQL's native ``uuid`` column type has no ``MAX()``
aggregate at all and raises ``UndefinedFunctionError``. This was caught
only by running the live endpoint against real PostgreSQL — the SQLite
unit tests in apps/api/tests/test_events_stream.py never exercised it.
"""

import os
import uuid

import pytest

from soa_api.dependencies import Dependencies
from soa_api.routers.events import _latest_event_id, _poll_once
from soa_api.settings import ApiSettings, Environment
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.outbox import enqueue_event
from soa_db.tenant_guard import bind_tenant

POSTGRES_URL = os.environ.get("SOA_TEST_POSTGRES_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="SOA_TEST_POSTGRES_URL not set; events-stream cursor test requires PostgreSQL",
    ),
]


async def test_latest_event_id_and_poll_work_against_real_postgres_uuid_columns() -> None:
    assert POSTGRES_URL is not None
    db = DatabaseSessions(create_database_engine(POSTGRES_URL))
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    organization_id = uuid.uuid4()

    async with db.session_scope() as session:
        await bind_tenant(session, organization_id)
        assert (
            await _latest_event_id(
                Dependencies(settings=ApiSettings(environment=Environment.TEST), db=db),
                organization_id,
            )
            is None
        )

    async with db.session_scope() as session:
        await bind_tenant(session, organization_id)
        first = await enqueue_event(
            session, event_type="a", payload={}, organization_id=organization_id
        )
        second = await enqueue_event(
            session, event_type="b", payload={}, organization_id=organization_id
        )

    deps = Dependencies(settings=ApiSettings(environment=Environment.TEST), db=db)
    latest = await _latest_event_id(deps, organization_id)
    assert latest == second.id

    rows = await _poll_once(deps, organization_id, first.id)
    assert [row.id for row in rows] == [second.id]
