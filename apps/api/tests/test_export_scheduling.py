"""Export scheduling tests (EXP-008, API side): duplicate approval
cannot double-schedule, and only deliverable integrations fan out."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_api.services.export_orchestration import (
    EXPORT_JOB_TYPE,
    _deliverable,
    schedule_exports_on_approval,
)
from soa_config import MemorySecretStore
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.documents import SourceChannel, create_document
from soa_db.exports import ExportJobRepository
from soa_db.integrations import (
    Integration,
    IntegrationStatus,
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
        deliverable.status = IntegrationStatus.ACTIVE
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


@pytest.mark.parametrize(
    ("integration_type", "expected"),
    [
        ("webhook", True),
        ("quickbooks_online", True),
        ("netsuite", False),
        ("microsoft_dynamics365", False),
        ("sap_s4hana", False),
    ],
)
def test_only_production_ready_connector_types_schedule(
    integration_type: str, expected: bool
) -> None:
    integration = Integration(
        name="ERP",
        slug="erp",
        integration_type=integration_type,
        status=IntegrationStatus.ACTIVE,
        endpoint_url="https://erp.example/orders",
        credential_id=uuid.uuid4(),
        active_mapping_version_id=uuid.uuid4(),
    )
    assert _deliverable(integration) is expected


async def test_active_quickbooks_integration_schedules_export(db: DatabaseSessions) -> None:
    """The ERP promise: an ACTIVE ``quickbooks_online`` integration is
    scheduled on approval exactly like a webhook. A fully configured ERP
    connector (endpoint, credential, published mapping) yields one
    export_jobs row and one queued ``EXPORT_JOB_TYPE`` job — the type gate
    is no longer webhook-only. The credential differs only in KIND: an
    OAuth2 access token rather than an HMAC secret."""
    async with db.session_scope() as session:
        document = await create_document(
            session,
            CONTEXT,
            stream_id=uuid.uuid4(),
            source_channel=SourceChannel.UPLOAD,
            original_filename="po.pdf",
            content_sha256="e" * 64,
            size_bytes=10,
            content_type="application/pdf",
            actor_id="user:test",
        )
        integration = await create_integration(
            session,
            CONTEXT,
            name="QuickBooks Online",
            slug="qbo",
            integration_type="quickbooks_online",
            endpoint_url="https://sandbox-quickbooks.api.intuit.com/v3/company/123",
            actor_id="user:test",
        )
        integration.status = IntegrationStatus.ACTIVE
        await store_integration_credential(
            session,
            CONTEXT,
            integration=integration,
            kind="oauth2_access_token",
            secret="qbo_access_token",
            actor_id="user:test",
            secret_store=MemorySecretStore(),
        )
        draft = await create_mapping_draft(
            session,
            CONTEXT,
            integration=integration,
            definition={"fields": [{"target": "DocNumber", "source": "identifiers.po_number"}]},
            actor_id="user:test",
        )
        await publish_mapping_draft(
            session, CONTEXT, integration=integration, draft=draft, actor_id="user:test"
        )

        scheduled = await schedule_exports_on_approval(
            session,
            CONTEXT,
            document=document,
            run_id=uuid.uuid4(),
            canonical_payload_id=uuid.uuid4(),
            actor_id="user:supervisor",
        )
        assert len(scheduled) == 1
        assert scheduled[0].integration_id == integration.id
        assert await ExportJobRepository(session, CONTEXT).count() == 1
        queue_jobs = (
            (await session.execute(select(Job).where(Job.job_type == EXPORT_JOB_TYPE)))
            .scalars()
            .all()
        )
        assert len(queue_jobs) == 1
        assert queue_jobs[0].payload["export_job_id"] == str(scheduled[0].id)
