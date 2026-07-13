"""Export scheduling at approval time (EXP-008, API side).

Approval fans out to every DELIVERABLE integration — active, webhook
endpoint configured, credential set, mapping published — in the SAME
transaction that completed the review. Exactly-once business intent
holds twice over: export jobs are idempotent on their business key
(EXP-005), and the queue job is deduped per export job, so a duplicate
approval event cannot double-deliver.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.documents import Document
from soa_db.exports import ExportJob, create_export_job
from soa_db.integrations import Integration, IntegrationRepository
from soa_db.jobs import enqueue_job
from soa_db.pagination import CursorRequest
from soa_db.repository import OrganizationContext

EXPORT_JOB_TYPE = "export.deliver"


def _deliverable(integration: Integration) -> bool:
    return (
        integration.status == "active"
        and integration.integration_type == "webhook"
        and integration.endpoint_url is not None
        and integration.credential_id is not None
        and integration.active_mapping_version_id is not None
    )


async def schedule_exports_on_approval(
    session: AsyncSession,
    context: OrganizationContext,
    *,
    document: Document,
    run_id: uuid.UUID,
    canonical_payload_id: uuid.UUID,
    actor_id: str,
) -> list[ExportJob]:
    """Create export jobs + queue work for every deliverable integration.
    Returns the jobs (existing ones included — creation is idempotent).
    No deliverable integrations is a valid outcome: the document simply
    stays approved."""
    integrations = await IntegrationRepository(session, context).list_page(CursorRequest(limit=100))
    scheduled: list[ExportJob] = []
    for integration in integrations.items:
        if not _deliverable(integration):
            continue
        assert integration.active_mapping_version_id is not None  # _deliverable
        job = await create_export_job(
            session,
            context,
            document_id=document.id,
            run_id=run_id,
            canonical_payload_id=canonical_payload_id,
            integration_id=integration.id,
            mapping_version_id=integration.active_mapping_version_id,
            actor_id=actor_id,
        )
        await enqueue_job(
            session,
            job_type=EXPORT_JOB_TYPE,
            payload={
                "export_job_id": str(job.id),
                "organization_id": str(context.organization_id),
            },
            organization_id=context.organization_id,
            dedupe_key=f"{EXPORT_JOB_TYPE}:{job.id}",
        )
        scheduled.append(job)
    return scheduled
