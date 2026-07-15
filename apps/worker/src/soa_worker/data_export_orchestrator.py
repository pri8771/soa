"""Bounded-batch execution for durable large data exports."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.data_export import collect_document_export
from soa_db.data_export_jobs import DataExportJobRepository, DataExportState
from soa_db.documents import Document
from soa_db.jobs import enqueue_job
from soa_db.repository import OrganizationContext
from soa_db.types import utcnow
from soa_storage import ObjectStore

BATCH_SIZE = 25


@dataclass(frozen=True)
class BatchResult:
    complete: bool
    processed: int


async def execute_data_export_batch(
    session: AsyncSession,
    store: ObjectStore,
    context: OrganizationContext,
    *,
    export_id: uuid.UUID,
) -> BatchResult:
    job = await DataExportJobRepository(session, context).get(export_id)
    if job is None:
        raise ValueError("data export job does not exist")
    if job.state in (DataExportState.SUCCEEDED, DataExportState.CANCELLED):
        return BatchResult(True, 0)
    if job.expires_at <= utcnow():
        job.state = DataExportState.CANCELLED
        job.safe_error = "export expired before completion"
        job.finished_at = utcnow()
        return BatchResult(True, 0)
    job.state = DataExportState.RUNNING
    base = select(Document.id).where(Document.organization_id == context.organization_id)
    if job.scope == "document":
        base = base.where(Document.id == job.document_id)
    if job.cursor_document_id is not None:
        base = base.where(Document.id > job.cursor_document_id)
    ids = list(
        (await session.execute(base.order_by(Document.id).limit(BATCH_SIZE))).scalars().all()
    )
    if job.total_documents == 0:
        count_stmt = select(func.count(Document.id)).where(
            Document.organization_id == context.organization_id
        )
        if job.scope == "document":
            count_stmt = count_stmt.where(Document.id == job.document_id)
        job.total_documents = int((await session.execute(count_stmt)).scalar_one())

    new_parts = list(job.parts)
    for document_id in ids:
        export = await collect_document_export(
            session,
            organization_id=context.organization_id,
            document_id=document_id,
        )
        payload = json.dumps(
            {
                "document_id": str(document_id),
                "records": export.records,
                "counts": export.counts(),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        digest = hashlib.sha256(payload).hexdigest()
        key = f"data-exports/{context.organization_id}/{job.id}/parts/{document_id}.json"
        await store.put(key, payload, content_type="application/json", sha256=digest)
        new_parts.append(
            {
                "document_id": str(document_id),
                "object_key": key,
                "sha256": digest,
                "bytes": len(payload),
                "records": export.total_records,
            }
        )
        job.total_records += export.total_records
        job.processed_documents += 1
        job.cursor_document_id = document_id
    job.parts = new_parts

    complete = len(ids) < BATCH_SIZE or job.processed_documents >= job.total_documents
    if complete:
        manifest = json.dumps(
            {
                "export_id": str(job.id),
                "organization_id": str(context.organization_id),
                "scope": job.scope,
                "total_documents": job.total_documents,
                "total_records": job.total_records,
                "parts": job.parts,
                "expires_at": job.expires_at.isoformat(),
            },
            sort_keys=True,
            indent=2,
        ).encode()
        key = f"data-exports/{context.organization_id}/{job.id}/manifest.json"
        await store.put(
            key,
            manifest,
            content_type="application/json",
            sha256=hashlib.sha256(manifest).hexdigest(),
        )
        job.manifest_object_key = key
        job.state = DataExportState.SUCCEEDED
        job.finished_at = utcnow()
    else:
        await enqueue_job(
            session,
            job_type="data_export.build",
            organization_id=context.organization_id,
            payload={
                "organization_id": str(context.organization_id),
                "data_export_id": str(job.id),
            },
            dedupe_key=f"data-export:{job.id}:after:{job.cursor_document_id}",
            max_attempts=5,
        )
    return BatchResult(complete, len(ids))
