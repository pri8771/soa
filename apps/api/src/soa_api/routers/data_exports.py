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

from soa_api.auth.authorization import AuthorizedContext
from soa_api.auth.dependency import require_permission
from soa_api.dependencies import DbSession, Dependencies, ObjectStoreDep, get_dependencies
from soa_db.audit import ActorType, record_audit_event
from soa_db.data_export import EXPORT_CATEGORIES, collect_document_export
from soa_db.documents import DocumentRepository
from soa_db.types import utcnow

router = APIRouter(tags=["data-exports"])


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
