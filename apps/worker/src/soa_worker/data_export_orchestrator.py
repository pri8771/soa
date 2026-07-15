"""Bounded-batch execution for durable large data exports."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.audit import ActorType, record_audit_event
from soa_db.data_export import EXPORT_CATEGORIES, collect_document_export
from soa_db.data_export_jobs import DataExportJob, DataExportJobRepository, DataExportState
from soa_db.documents import Document
from soa_db.jobs import enqueue_job
from soa_db.organization_export import (
    ORGANIZATION_EXPORT_CATEGORIES,
    OrganizationExportCategory,
    collect_organization_export_page,
)
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_db.types import utcnow
from soa_storage import ObjectNotFoundError, ObjectStore

BATCH_SIZE = 25
CONTROL_PLANE_BATCH_SIZE = 250


@dataclass(frozen=True)
class BatchResult:
    complete: bool
    processed: int


def _control_parts(parts: list[object], category: str) -> list[dict[str, object]]:
    return [
        part
        for part in parts
        if isinstance(part, dict)
        and part.get("part_type") == "organization_records"
        and part.get("category") == category
    ]


def _next_control_category(job_parts: list[object]) -> OrganizationExportCategory | None:
    for category in ORGANIZATION_EXPORT_CATEGORIES:
        parts = _control_parts(job_parts, category.key)
        if not parts or not bool(parts[-1].get("complete")):
            return category
    return None


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _category_manifest(parts: list[object]) -> list[dict[str, object]]:
    """Build the closed category/count inventory exposed by the manifest."""

    categories: list[dict[str, object]] = []
    for control_category in ORGANIZATION_EXPORT_CATEGORIES:
        category_parts = _control_parts(parts, control_category.key)
        categories.append(
            {
                "category": control_category.key,
                "description": control_category.description,
                "record_count": sum(_as_int(part.get("records", 0)) for part in category_parts),
                "part_count": sum(1 for part in category_parts if part.get("object_key")),
                "source": "organization",
            }
        )
    for document_category in EXPORT_CATEGORIES:
        document_parts = [
            part
            for part in parts
            if isinstance(part, dict) and part.get("part_type") == "document_records"
        ]
        categories.append(
            {
                "category": document_category.key,
                "description": document_category.description,
                "record_count": sum(
                    int(
                        part.get("category_counts", {}).get(document_category.key, 0)
                        if isinstance(part.get("category_counts"), dict)
                        else 0
                    )
                    for part in document_parts
                ),
                "part_count": sum(
                    1
                    for part in document_parts
                    if isinstance(part.get("category_counts"), dict)
                    and _as_int(part["category_counts"].get(document_category.key, 0)) > 0
                ),
                "source": "documents",
            }
        )
    return categories


def _public_part_metadata(parts: list[object]) -> list[dict[str, object]]:
    """Remove storage coordinates and internal cursors from downloadable metadata."""

    return [
        {
            str(key): value
            for key, value in part.items()
            if key not in {"object_key", "cursor", "complete"}
        }
        for part in parts
        if isinstance(part, dict) and part.get("object_key")
    ]


async def _enqueue_next_batch(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    export_id: uuid.UUID,
    progress_token: str,
) -> None:
    await enqueue_job(
        session,
        job_type="data_export.build",
        organization_id=organization_id,
        payload={
            "organization_id": str(organization_id),
            "data_export_id": str(export_id),
        },
        dedupe_key=f"data-export:{export_id}:after:{progress_token}",
        max_attempts=5,
    )


async def _put_json_retry_safe(
    store: ObjectStore,
    *,
    key: str,
    payload: bytes,
    digest: str,
) -> None:
    """Create a deterministic part or accept an identical prior retry write.

    Object storage and the database are not one transaction. A worker can
    therefore write an object and lose its DB commit. Retrying must preserve an
    identical object; a different digest at the stable key fails closed instead
    of silently replacing export evidence.
    """

    try:
        existing = await store.head(key)
    except ObjectNotFoundError:
        await store.put(key, payload, content_type="application/json", sha256=digest)
        return
    if existing.sha256 != digest or existing.size != len(payload):
        raise ValueError(f"data export retry object {key!r} differs from the original write")


async def _execute_control_plane_batch(
    session: AsyncSession,
    store: ObjectStore,
    context: OrganizationContext,
    *,
    job: DataExportJob,
) -> BatchResult | None:
    """Write one resumable page of one allowlisted organization category.

    Empty categories are marked complete in the same claim so a sparse tenant
    does not pay dozens of queue round trips.  The function yields after the
    first non-empty page, keeping record and object-memory work bounded.
    """

    parts = list(job.parts)
    while (category := _next_control_category(parts)) is not None:
        prior_parts = _control_parts(parts, category.key)
        cursor_value = prior_parts[-1].get("cursor") if prior_parts else None
        after_id = str(cursor_value) if cursor_value is not None else None
        page = await collect_organization_export_page(
            session,
            organization_id=context.organization_id,
            snapshot_at=job.snapshot_at,
            category=category,
            after_id=after_id,
            limit=CONTROL_PLANE_BATCH_SIZE,
        )
        sequence = len(prior_parts) + 1
        part: dict[str, object] = {
            "part_type": "organization_records",
            "category": category.key,
            "name": f"organization/{category.key}/part-{sequence:06d}.json",
            "sequence": sequence,
            "records": len(page.records),
            "cursor": page.last_id,
            "complete": page.complete,
        }
        if page.records:
            payload = json.dumps(
                {
                    "category": category.key,
                    "description": category.description,
                    "snapshot_at": job.snapshot_at.isoformat(),
                    "record_count": len(page.records),
                    "records": page.records,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
            digest = hashlib.sha256(payload).hexdigest()
            key = (
                f"data-exports/{context.organization_id}/{job.id}/"
                f"organization/{category.key}/part-{sequence:06d}.json"
            )
            # Organization rows are snapshotted; release SQL for the object
            # store round trip. Stable keys/digests make a crash retry safe.
            await session.commit()
            await _put_json_retry_safe(store, key=key, payload=payload, digest=digest)
            await bind_tenant(session, context.organization_id)
            job_id = job.id
            session.expire(job)
            reloaded = await DataExportJobRepository(session, context).get(job_id, for_update=True)
            if reloaded is None:
                raise ValueError("data export disappeared while writing a part")
            job = reloaded
            parts = list(job.parts)
            part.update(
                {
                    "object_key": key,
                    "sha256": digest,
                    "bytes": len(payload),
                }
            )
        parts.append(part)
        job.parts = parts
        job.total_records += len(page.records)
        if page.records:
            progress = f"organization:{category.key}:{sequence}:{page.last_id}"
            await _enqueue_next_batch(
                session,
                organization_id=context.organization_id,
                export_id=job.id,
                progress_token=progress,
            )
            return BatchResult(False, len(page.records))
        if not page.complete:
            raise RuntimeError("empty organization export page unexpectedly has more rows")
    return None


async def delete_export_objects(
    store: ObjectStore,
    object_keys: list[str],
    *,
    organization_id: uuid.UUID,
) -> int:
    """Delete a tenant's bounded export-object list idempotently."""

    deleted = 0
    tenant_prefix = f"data-exports/{organization_id}/"
    for key in dict.fromkeys(object_keys):
        if not key.startswith(tenant_prefix):
            raise ValueError("data export cleanup key does not belong to its tenant")
        try:
            await store.delete(key)
        except ObjectNotFoundError:
            continue
        deleted += 1
    return deleted


async def cleanup_expired_data_export(
    session: AsyncSession,
    store: ObjectStore,
    context: OrganizationContext,
    *,
    export_id: uuid.UUID,
) -> int:
    """Delete expired durable-export artifacts and retain counts-only evidence."""

    job = await DataExportJobRepository(session, context).get(export_id)
    if job is None:
        return 0
    if job.expires_at > utcnow():
        raise ValueError("data export cleanup ran before its retention deadline")
    object_keys = [
        str(part["object_key"])
        for part in job.parts
        if isinstance(part, dict) and part.get("object_key")
    ]
    if job.manifest_object_key:
        object_keys.append(job.manifest_object_key)
    await session.commit()
    deleted = await delete_export_objects(
        store,
        object_keys,
        organization_id=context.organization_id,
    )
    await bind_tenant(session, context.organization_id)
    job_id = job.id
    session.expire(job)
    reloaded = await DataExportJobRepository(session, context).get(job_id, for_update=True)
    if reloaded is None:
        return deleted
    job = reloaded
    retained_counts = {
        "parts": len(job.parts),
        "objects_deleted": deleted,
        "total_documents": job.total_documents,
        "total_records": job.total_records,
        "categories": {
            str(category["category"]): _as_int(category["record_count"])
            for category in _category_manifest(list(job.parts))
        },
    }
    job.parts = []
    job.manifest_object_key = None
    job.state = DataExportState.EXPIRED
    job.safe_error = "export retention window expired; generated objects were deleted"
    job.finished_at = utcnow()
    await record_audit_event(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id="system:data-export-cleanup",
        action="organization.data_export_expired",
        target_type="data_export_job",
        target_id=str(job.id),
        organization_id=context.organization_id,
        summary=retained_counts,
    )
    return deleted


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
    if job.state in (
        DataExportState.SUCCEEDED,
        DataExportState.CANCELLED,
        DataExportState.EXPIRED,
    ):
        return BatchResult(True, 0)
    if job.expires_at <= utcnow():
        job.state = DataExportState.CANCELLED
        job.safe_error = "export expired before completion"
        job.finished_at = utcnow()
        return BatchResult(True, 0)
    job.state = DataExportState.RUNNING
    if not job.parts:
        count_stmt = select(func.count(Document.id)).where(
            Document.organization_id == context.organization_id,
            Document.created_at <= job.snapshot_at,
        )
        if job.scope == "document":
            count_stmt = count_stmt.where(Document.id == job.document_id)
        job.total_documents = int((await session.execute(count_stmt)).scalar_one())
    if job.scope == "organization":
        control_result = await _execute_control_plane_batch(
            session,
            store,
            context,
            job=job,
        )
        if control_result is not None:
            return control_result
    base = select(Document.id).where(Document.organization_id == context.organization_id)
    base = base.where(Document.created_at <= job.snapshot_at)
    if job.scope == "document":
        base = base.where(Document.id == job.document_id)
    if job.cursor_document_id is not None:
        base = base.where(Document.id > job.cursor_document_id)
    ids = list(
        (await session.execute(base.order_by(Document.id).limit(BATCH_SIZE))).scalars().all()
    )
    if job.total_documents == 0:
        count_stmt = select(func.count(Document.id)).where(
            Document.organization_id == context.organization_id,
            Document.created_at <= job.snapshot_at,
        )
        if job.scope == "document":
            count_stmt = count_stmt.where(Document.id == job.document_id)
        job.total_documents = int((await session.execute(count_stmt)).scalar_one())

    prepared_parts: list[tuple[uuid.UUID, bytes, str, str, int, dict[str, int]]] = []
    for document_id in ids:
        export = await collect_document_export(
            session,
            organization_id=context.organization_id,
            document_id=document_id,
            snapshot_at=job.snapshot_at,
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
        prepared_parts.append(
            (document_id, payload, digest, key, export.total_records, export.counts())
        )

    # All database reads for this batch are complete. Storage latency runs
    # without a SQL transaction; deterministic keys let a retry accept any
    # identical objects written before a crash.
    await session.commit()
    for _document_id, payload, digest, key, _total_records, _counts in prepared_parts:
        await _put_json_retry_safe(store, key=key, payload=payload, digest=digest)

    await bind_tenant(session, context.organization_id)
    job_id = job.id
    session.expire(job)
    reloaded = await DataExportJobRepository(session, context).get(job_id, for_update=True)
    if reloaded is None:
        raise ValueError("data export disappeared while writing document parts")
    job = reloaded
    new_parts = list(job.parts)
    for document_id, payload, digest, key, total_records, counts in prepared_parts:
        new_parts.append(
            {
                "part_type": "document_records",
                "category": "document_bundle",
                "name": f"documents/{document_id}.json",
                "document_id": str(document_id),
                "object_key": key,
                "sha256": digest,
                "bytes": len(payload),
                "records": total_records,
                "category_counts": counts,
            }
        )
        job.total_records += total_records
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
                "snapshot_at": job.snapshot_at.isoformat(),
                "total_documents": job.total_documents,
                "total_records": job.total_records,
                "categories": _category_manifest(list(job.parts)),
                "parts": _public_part_metadata(list(job.parts)),
                "expires_at": job.expires_at.isoformat(),
                "retention": {
                    "expires_at": job.expires_at.isoformat(),
                    "cleanup": "Export objects are deleted after this timestamp; counts-only "
                    "audit evidence is retained.",
                },
                "snapshot_boundary": (
                    "Records created (or audit events occurring) on or before snapshot_at are "
                    "eligible. Every part is tenant-scoped and uses the same immutable boundary."
                ),
            },
            sort_keys=True,
            indent=2,
        ).encode()
        key = f"data-exports/{context.organization_id}/{job.id}/manifest.json"
        # Persist completed part progress before publishing the manifest. If
        # the put fails, the next delivery reconstructs the same manifest.
        await session.commit()
        await _put_json_retry_safe(
            store,
            key=key,
            payload=manifest,
            digest=hashlib.sha256(manifest).hexdigest(),
        )
        await bind_tenant(session, context.organization_id)
        job_id = job.id
        session.expire(job)
        reloaded = await DataExportJobRepository(session, context).get(job_id, for_update=True)
        if reloaded is None:
            raise ValueError("data export disappeared while writing its manifest")
        job = reloaded
        job.manifest_object_key = key
        job.state = DataExportState.SUCCEEDED
        job.finished_at = utcnow()
    else:
        await _enqueue_next_batch(
            session,
            organization_id=context.organization_id,
            export_id=job.id,
            progress_token=f"document:{job.cursor_document_id}",
        )
    return BatchResult(complete, len(ids))
