"""Customer data export workflow (SEC-009).

One endpoint: build a scoped, signed, expiring export bundle of
everything the platform holds for a single document — the portability /
subject-access unit. Reuses the ANA-007 bundle discipline: JSON files
plus a manifest carrying each file's SHA-256 and byte size, written to
object storage, handed out ONLY as short-lived signed URLs, and audited
counts-only.

Authorization is the central path (``data.export`` on a membership in
the document's organization). The manifest documents EVERY data category
(with counts, including empty ones), states plainly that artifact FILE
bytes are delivered through signed downloads rather than copied into the
bundle, and records the retention posture. Bundles build synchronously
under the per-document scale; the queued-job path for organization-wide
exports arrives when the worker claim loop is wired (documented, not
faked).
"""

import hashlib
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, Dependencies, ObjectStoreDep, get_dependencies
from soa_db.audit import ActorType, record_audit_event
from soa_db.data_export import EXPORT_CATEGORIES, collect_document_export
from soa_db.data_export_jobs import (
    DataExportJob,
    DataExportJobRepository,
    DataExportState,
    new_data_export_job,
)
from soa_db.documents import DocumentRepository
from soa_db.jobs import enqueue_job
from soa_db.types import utcnow

router = APIRouter(tags=["data-exports"])


class CancelExportRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


async def _durable_response(job: DataExportJob, store: Any, ttl: int) -> dict[str, Any]:
    manifest_url = None
    manifest_expires_at = None
    if (
        job.state == DataExportState.SUCCEEDED
        and job.manifest_object_key
        and job.expires_at > utcnow()
    ):
        signed = await store.signed_download_url(
            job.manifest_object_key,
            expires_in_seconds=min(ttl, max(1, int((job.expires_at - utcnow()).total_seconds()))),
        )
        manifest_url = signed.url
        manifest_expires_at = signed.expires_at.isoformat()
    return {
        "id": str(job.id),
        "scope": job.scope,
        "state": job.state,
        "total_documents": job.total_documents,
        "processed_documents": job.processed_documents,
        "progress": (
            round(job.processed_documents / job.total_documents, 4) if job.total_documents else 0.0
        ),
        "total_records": job.total_records,
        "safe_error": job.safe_error,
        "expires_at": job.expires_at.isoformat(),
        "manifest_download_url": manifest_url,
        "manifest_expires_at": manifest_expires_at,
    }


@router.post(
    "/orgs/{organization_slug}/data-exports",
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_organization_data_export(
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.export"))],
    session: DbSession,
    store: ObjectStoreDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    job = new_data_export_job(
        organization_id=authorized.org_context.organization_id,
        scope="organization",
        created_by=authorized.principal.subject,
    )
    session.add(job)
    await session.flush()
    await enqueue_job(
        session,
        job_type="data_export.build",
        organization_id=authorized.org_context.organization_id,
        payload={
            "organization_id": str(authorized.org_context.organization_id),
            "data_export_id": str(job.id),
        },
        dedupe_key=f"data-export:{job.id}:start",
        max_attempts=5,
    )
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=authorized.principal.subject,
        action="organization.data_export_requested",
        target_type="data_export_job",
        target_id=str(job.id),
        organization_id=authorized.org_context.organization_id,
        summary={"scope": "organization", "expires_at": job.expires_at.isoformat()},
    )
    return await _durable_response(job, store, deps.settings.download_url_ttl_seconds)


@router.get("/orgs/{organization_slug}/data-exports/{export_id}")
async def get_durable_data_export(
    export_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.export"))],
    session: DbSession,
    store: ObjectStoreDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    job = await DataExportJobRepository(session, authorized.org_context).get(export_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Data export not found.")
    return await _durable_response(job, store, deps.settings.download_url_ttl_seconds)


@router.post("/orgs/{organization_slug}/data-exports/{export_id}/cancel")
async def cancel_durable_data_export(
    export_id: uuid.UUID,
    body: CancelExportRequest,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.export"))],
    session: DbSession,
    store: ObjectStoreDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    job = await DataExportJobRepository(session, authorized.org_context).get(export_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Data export not found.")
    if job.state not in (DataExportState.PENDING, DataExportState.RUNNING):
        raise HTTPException(status_code=409, detail="Only pending or running exports cancel.")
    job.state = DataExportState.CANCELLED
    job.safe_error = f"cancelled by operator: {body.reason}"
    job.finished_at = utcnow()
    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=authorized.principal.subject,
        action="organization.data_export_cancelled",
        target_type="data_export_job",
        target_id=str(job.id),
        organization_id=authorized.org_context.organization_id,
        summary={"reason": body.reason, "processed_documents": job.processed_documents},
    )
    return await _durable_response(job, store, deps.settings.download_url_ttl_seconds)


@router.post(
    "/orgs/{organization_slug}/documents/{document_id}/data-exports",
    status_code=status.HTTP_201_CREATED,
)
async def create_document_data_export(
    document_id: uuid.UUID,
    authorized: Annotated[AuthorizedContext, Depends(require_permission("data.export"))],
    session: DbSession,
    store: ObjectStoreDep,
    deps: Annotated[Dependencies, Depends(get_dependencies)],
) -> dict[str, Any]:
    """Build a signed, expiring data-export bundle for one document."""
    organization_id = authorized.org_context.organization_id
    # Tenant scope first: a document outside the caller's organization is
    # indistinguishable from one that does not exist.
    document = await DocumentRepository(session, authorized.org_context).get(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    export = await collect_document_export(
        session, organization_id=organization_id, document_id=document_id
    )
    counts = export.counts()

    export_id = uuid.uuid4()
    prefix = f"data-exports/{organization_id}/{document_id}/{export_id}"
    ttl = deps.settings.download_url_ttl_seconds
    files: list[dict[str, Any]] = []
    for category in EXPORT_CATEGORIES:
        records = export.records.get(category.key, [])
        payload = json.dumps(
            {
                "category": category.key,
                "description": category.description,
                "record_count": len(records),
                "records": records,
            },
            sort_keys=True,
            indent=2,
            default=str,
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        name = f"{category.key}.json"
        key = f"{prefix}/{name}"
        await store.put(key, payload, content_type="application/json", sha256=digest)
        signed = await store.signed_download_url(key, expires_in_seconds=ttl)
        files.append(
            {
                "category": category.key,
                "name": name,
                "record_count": len(records),
                "sha256": digest,
                "bytes": len(payload),
                "download_url": signed.url,
                "expires_at": signed.expires_at.isoformat(),
            }
        )

    manifest = {
        "export_id": str(export_id),
        "document_id": str(document_id),
        "organization_id": str(organization_id),
        "generated_at": utcnow().isoformat(),
        "generated_by": f"user:{authorized.membership.user_id}",
        "categories": [
            {
                "category": category.key,
                "description": category.description,
                "record_count": counts[category.key],
            }
            for category in EXPORT_CATEGORIES
        ],
        "total_records": export.total_records,
        "notes": (
            "Artifact FILE bytes (original and derived documents) are delivered "
            "through short-lived signed download URLs per artifact, not copied "
            "into this bundle; the 'artifacts' category lists their metadata and "
            "object keys. All download URLs in this export expire."
        ),
        "files": [
            {key: value for key, value in entry.items() if key != "download_url"} for entry in files
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_key = f"{prefix}/manifest.json"
    await store.put(
        manifest_key, manifest_bytes, content_type="application/json", sha256=manifest_sha256
    )
    manifest_signed = await store.signed_download_url(manifest_key, expires_in_seconds=ttl)

    await record_audit_event(
        session,
        actor_type=ActorType.USER,
        actor_id=f"user:{authorized.membership.user_id}",
        action="document.data_export_created",
        target_type="document",
        target_id=str(document_id),
        organization_id=organization_id,
        # Counts only — never the exported content itself.
        summary={
            "export_id": str(export_id),
            "total_records": export.total_records,
            "counts": counts,
            "manifest_sha256": manifest_sha256,
        },
    )
    return {
        "export_id": str(export_id),
        "document_id": str(document_id),
        "total_records": export.total_records,
        "counts": counts,
        "manifest_sha256": manifest_sha256,
        "manifest_download_url": manifest_signed.url,
        "manifest_expires_at": manifest_signed.expires_at.isoformat(),
        "files": files,
    }
