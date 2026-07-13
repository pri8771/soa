"""Export scheduling tests (EXP-008, API side): duplicate approval
cannot double-schedule, and only deliverable integrations fan out."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_api.services.export_orchestration import schedule_exports_on_approval
from soa_config import MemorySecretStore
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import SourceChannel, create_document
from soa_db.exports import ExportJobRepository
from soa_db.integrations import (
    create_integration,
    create_mapping_draft,
    publish_mapping_draft,
    store_integration_credential,
)
from soa_db.jobs import Job
from soa_db.repository import OrganizationContext

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONTEXT = OrganizationContext(organization_id=ORG)


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/export-sched.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


async def test_duplicate_approval_schedules_exactly_one_export(db: DatabaseSessions) -> None:
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256="d" * 64,
            size_bytes=10,
            content_type="application/pdf",
            actor_id="user:test",
        )
        # One deliverable integration...
        deliverable = await create_integration(
            session,
            CONTEXT,
            name="ERP",
            slug="erp",
            integration_type="webhook",
            endpoint_url="https://erp.northstar.example/orders",
            actor_id="user:test",
        )
        await store_integration_credential(
            session,
            CONTEXT,
            integration=deliverable,
            kind="webhook_hmac_secret",
            secret="whsec_scheduling",
            actor_id="user:test",
            secret_store=MemorySecretStore(),
        )
        draft = await create_mapping_draft(
            session,
            CONTEXT,
            integration=deliverable,
            definition={"fields": [{"target": "PoNumber", "source": "identifiers.po_number"}]},
            actor_id="user:test",
        )
        await publish_mapping_draft(
            session, CONTEXT, integration=deliverable, draft=draft, actor_id="user:test"
        )
        # ...and one that is NOT deliverable (no credential, no mapping).
        await create_integration(
            session,
            CONTEXT,
            name="Half-configured",
            slug="half",
            integration_type="webhook",
            endpoint_url="https://half.northstar.example/x",
            actor_id="user:test",
        )

        run_id = uuid.uuid4()
        payload_id = uuid.uuid4()
        first = await schedule_exports_on_approval(
            session,
            CONTEXT,
            document=document,
            run_id=run_id,
            canonical_payload_id=payload_id,
            actor_id="user:supervisor",
        )
        assert len(first) == 1  # only the deliverable integration

        # A DUPLICATE approval event schedules nothing new: same export
        # job (business key) and no second queue job (dedupe key).
        second = await schedule_exports_on_approval(
            session,
            CONTEXT,
            document=document,
            run_id=run_id,
            canonical_payload_id=payload_id,
            actor_id="user:supervisor",
        )
        assert [job.id for job in second] == [job.id for job in first]
        assert await ExportJobRepository(session, CONTEXT).count() == 1
        queue_jobs = (
            (await session.execute(select(Job).where(Job.job_type == "export.deliver")))
            .scalars()
            .all()
        )
        assert len(queue_jobs) == 1
        assert queue_jobs[0].payload["export_job_id"] == str(first[0].id)
